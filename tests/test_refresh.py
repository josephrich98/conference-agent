"""Offline tests for the per-conference auto-check policy (``refresh`` module).

The predicate :func:`is_due_for_check` is pure, so most cases construct a bare
``ConferenceRow`` and pin ``today`` for determinism. A couple of cases exercise
the database-backed selection/marking helpers against a temporary SQLite file.
"""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from conference_agent.database import ConferenceRow, get_engine, upsert_conferences
from conference_agent.models import Conference
from conference_agent.page_watch import PageCheck
from conference_agent.refresh import (
    _add_months,
    _decide,
    due_subcategories,
    edition_anchor,
    is_due_for_check,
    is_retired,
    mark_subcategories_checked,
    plan_watch,
    run_watch,
    watch_tier,
)

TODAY = date(2026, 6, 17)


def _row(**overrides) -> ConferenceRow:
    base = dict(id="X", acronym="X", name="X", subcategory="radiology")
    base.update(overrides)
    return ConferenceRow(**base)


def test_in_window_never_checked_is_due():
    # Prior edition 8 months ago, no upcoming announced, never checked.
    row = _row(prior_start_date=date(2025, 10, 17))
    assert is_due_for_check(row, TODAY) is True


def test_too_soon_is_not_due():
    # Edition only ~3 months ago -> next dates unlikely to be out yet.
    row = _row(prior_start_date=date(2026, 3, 17))
    assert is_due_for_check(row, TODAY) is False


def test_past_two_years_is_not_due():
    # Edition over two years ago -> assume dead or infrequent, stop checking.
    row = _row(prior_start_date=date(2024, 5, 17))
    assert is_due_for_check(row, TODAY) is False


def test_retired_only_past_the_check_window():
    # Retired once the last edition is over two years old with nothing newer.
    assert is_retired(_row(prior_start_date=date(2024, 5, 17)), TODAY) is True
    assert is_retired(_row(prior_start_date=date(2025, 5, 17)), TODAY) is False
    # A future edition on record, or no dates at all (fresh seed), never retires.
    assert is_retired(
        _row(prior_start_date=date(2024, 5, 17), upcoming_start_date=date(2026, 9, 1)),
        TODAY,
    ) is False
    assert is_retired(_row(), TODAY) is False


def test_biennial_gap_is_still_due():
    # Edition ~13 months ago: past the old one-year cutoff, inside the two-year one.
    row = _row(prior_start_date=date(2025, 5, 17))
    assert is_due_for_check(row, TODAY) is True


def test_window_is_anchored_on_the_submission_deadline():
    # Meeting only 3 months ago, but its abstract call closed 8 months ago: the
    # window is measured from the deadline, so the series is already due.
    row = _row(prior_abstract_deadline=date(2025, 10, 17), prior_start_date=date(2026, 3, 17))
    assert is_due_for_check(row, TODAY) is True
    assert edition_anchor(row) == date(2025, 10, 17)


def test_future_deadline_alone_counts_as_updated():
    # Next edition's call is announced (deadline ahead) but no meeting dates yet.
    row = _row(prior_start_date=date(2025, 10, 1), upcoming_abstract_deadline=date(2026, 8, 1))
    assert is_due_for_check(row, TODAY) is False


def test_future_upcoming_is_updated_not_due():
    # An upcoming edition is on record in the future -> already updated.
    row = _row(
        prior_start_date=date(2025, 10, 1),
        upcoming_start_date=date(2026, 11, 1),
    )
    assert is_due_for_check(row, TODAY) is False


def test_passed_upcoming_anchors_the_window():
    # The upcoming edition has come and gone (not yet rolled to prior); it is the
    # most recent edition, so it -- not the prior date -- anchors the window.
    row = _row(
        prior_start_date=date(2024, 11, 1),
        upcoming_start_date=date(2025, 11, 1),  # ~7 months ago
    )
    assert is_due_for_check(row, TODAY) is True


def test_recheck_interval_gates_repeat_checks():
    anchor = date(2025, 10, 17)  # 8 months ago -> in window
    just_checked = _row(prior_start_date=anchor, last_checked=TODAY - timedelta(days=5))
    assert is_due_for_check(just_checked, TODAY) is False  # checked 5 days ago

    stale_check = _row(prior_start_date=anchor, last_checked=TODAY - timedelta(days=14))
    assert is_due_for_check(stale_check, TODAY) is True  # interval elapsed


def test_no_dates_checked_once_then_left_alone():
    fresh = _row()  # never checked, no dates -> initial pass
    assert is_due_for_check(fresh, TODAY) is True

    already = _row(last_checked=TODAY - timedelta(days=400))  # checked, still dateless
    assert is_due_for_check(already, TODAY) is False


def test_add_months_clamps_day():
    assert _add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert _add_months(date(2026, 1, 15), 12) == date(2027, 1, 15)


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def test_due_subcategories_and_marking_round_trip(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            # Due: prior edition 8 months ago, no upcoming.
            Conference(acronym="A", name="A", subcategory="radiology",
                       prior_start_date=date(2025, 10, 17)),
            # Updated: future upcoming edition on record.
            Conference(acronym="B", name="B", subcategory="cardiology",
                       upcoming_start_date=date(2026, 11, 1)),
        ],
        db_url=url,
    )

    assert due_subcategories(url, TODAY) == ["radiology"]

    # Stamping the refreshed field records the check for every row in it, so the
    # series is no longer due until the interval elapses.
    assert mark_subcategories_checked(["radiology"], db_url=url, today=TODAY) == 1
    assert due_subcategories(url, TODAY) == []
    assert due_subcategories(url, TODAY + timedelta(days=14)) == ["radiology"]


# --- Watch cadence -------------------------------------------------------------


def test_watch_tier_daily_around_a_deadline():
    for offset in (-14, -3, 0, 10, 14):
        row = _row(upcoming_abstract_deadline=TODAY + timedelta(days=offset))
        assert watch_tier(row, TODAY) == "daily", offset
    # The late abstract and paper deadlines count too.
    assert watch_tier(_row(upcoming_paper_deadline=TODAY - timedelta(days=5)), TODAY) == "daily"


def test_watch_tier_soon_for_next_month():
    assert watch_tier(_row(upcoming_abstract_deadline=TODAY + timedelta(days=25)), TODAY) == "soon"
    assert watch_tier(_row(upcoming_start_date=TODAY + timedelta(days=30)), TODAY) == "soon"
    # Further out, with nothing else pending -> not watched.
    assert watch_tier(_row(upcoming_start_date=TODAY + timedelta(days=45)), TODAY) is None
    # A deadline 20 days past is neither daily nor upcoming.
    assert watch_tier(_row(upcoming_abstract_deadline=TODAY - timedelta(days=20)), TODAY) is None


def test_watch_tier_stale_and_initial():
    assert watch_tier(_row(prior_start_date=date(2025, 10, 17)), TODAY) == "stale"
    assert watch_tier(_row(), TODAY) == "initial"
    assert watch_tier(_row(last_checked=TODAY), TODAY) is None


def _check(fp):
    return PageCheck(fp, dates=3, pages=1) if fp else PageCheck(None, error="HTTP 403")


def test_decide_gates_research_on_page_change():
    recent = TODAY - timedelta(days=3)
    # Unchanged page, researched recently -> skip.
    row = _row(watch_fingerprint="a", last_checked=recent)
    assert _decide(row, "daily", _check("a"), TODAY).run_agent is False
    # Changed page -> research regardless of how recently.
    d = _decide(row, "daily", _check("b"), TODAY)
    assert (d.status, d.run_agent) == ("changed", True)
    # Unchanged page but past the backstop -> research.
    old = _row(watch_fingerprint="a", last_checked=TODAY - timedelta(days=28))
    assert _decide(old, "stale", _check("a"), TODAY).run_agent is True


def test_decide_falls_back_to_interval_when_unreadable_or_new():
    fresh = _row(last_checked=TODAY - timedelta(days=3))
    stale = _row(last_checked=TODAY - timedelta(days=14))
    assert _decide(fresh, "daily", _check(None), TODAY).run_agent is False
    assert _decide(stale, "daily", _check(None), TODAY).run_agent is True
    assert _decide(fresh, "soon", None, TODAY).status == "unreadable"  # no link
    # First observation stores a baseline; research follows the same interval rule.
    assert _decide(fresh, "soon", _check("a"), TODAY).status == "baseline"
    # A refresh that replaced the official link re-baselines rather than
    # reporting the (unrelated) old page's fingerprint as a change.
    moved = _row(watch_fingerprint="a", watch_url="https://old.org", url="https://new.org",
                 last_checked=TODAY - timedelta(days=3))
    assert _decide(moved, "daily", _check("b"), TODAY).status == "baseline"
    assert _decide(moved, "daily", _check("b"), TODAY).run_agent is False
    assert _decide(fresh, "soon", _check("a"), TODAY).run_agent is False
    assert _decide(stale, "soon", _check("a"), TODAY).run_agent is True


def _seed_watch_db(url):
    upsert_conferences(
        [
            # daily tier: abstract deadline in 5 days
            Conference(acronym="D", name="D", subcategory="radiology", url="https://d.org",
                       upcoming_abstract_deadline=TODAY + timedelta(days=5),
                       upcoming_start_date=TODAY + timedelta(days=120), attendance=5000),
            # stale tier
            Conference(acronym="S", name="S", subcategory="cardiology", url="https://s.org",
                       prior_start_date=date(2025, 10, 17)),
            # not watched: next edition far off
            Conference(acronym="F", name="F", subcategory="oncology", url="https://f.org",
                       upcoming_start_date=TODAY + timedelta(days=200)),
        ],
        db_url=url,
    )


def _rows(url):
    with Session(get_engine(url)) as session:
        return {r.id: r for r in session.query(ConferenceRow)}


def test_plan_watch_selects_tiers_and_respects_biweekly_interval(tmp_path):
    url = _db_url(tmp_path)
    _seed_watch_db(url)
    fetched = []

    def check(u):
        fetched.append(u)
        return _check("fp")

    decisions = plan_watch(url, TODAY, check)
    assert [(d.id, d.tier) for d in decisions] == [("d", "daily"), ("s", "stale")]
    assert sorted(fetched) == ["https://d.org", "https://s.org"]

    # After a run records the checks, the stale series waits two weeks; the
    # daily one is checked again tomorrow.
    run_watch(url, today=TODAY, check=check, refresh=lambda targets, attendance_hints=None: [], log=lambda m: None)
    tomorrow = TODAY + timedelta(days=1)
    assert [d.id for d in plan_watch(url, tomorrow, check)] == ["d"]
    assert {d.id for d in plan_watch(url, TODAY + timedelta(days=14), check)} == {"d", "s"}


def test_run_watch_merges_results_and_records_state(tmp_path):
    url = _db_url(tmp_path)
    _seed_watch_db(url)
    extended = TODAY + timedelta(days=12)

    def refresh(targets, attendance_hints=None):
        assert {t.id for t in targets} == {"d", "s"}
        # D's deadline was extended; the result omits attendance, which must survive.
        return [Conference(acronym="D", name="D", subcategory="radiology",
                           upcoming_abstract_deadline=extended)]

    report = run_watch(url, today=TODAY, check=lambda u: _check("fp1"), refresh=refresh, log=lambda m: None)
    assert report.researched == ["d", "s"]
    assert report.changes["d"] == [
        f"upcoming_abstract_deadline: {TODAY + timedelta(days=5)} -> {extended}"
    ]
    rows = _rows(url)
    assert rows["d"].upcoming_abstract_deadline == extended
    assert rows["d"].attendance == 5000
    assert (rows["d"].last_checked, rows["d"].watch_checked, rows["d"].watch_fingerprint) == (
        TODAY, TODAY, "fp1"
    )
    assert rows["d"].watch_url == "https://d.org"

    # Next day, same page -> no research.
    nxt = run_watch(url, today=TODAY + timedelta(days=1), check=lambda u: _check("fp1"),
                    refresh=refresh, log=lambda m: None)
    assert nxt.researched == []


def test_run_watch_leaves_failed_batches_for_the_next_run(tmp_path):
    url = _db_url(tmp_path)
    _seed_watch_db(url)

    def boom(targets, attendance_hints=None):
        raise RuntimeError("agent down")

    report = run_watch(url, today=TODAY, check=lambda u: _check("fp1"), refresh=boom, log=lambda m: None)
    assert report.failed == ["d", "s"]
    rows = _rows(url)
    # Nothing recorded, so tomorrow's run re-plans both with the same evidence.
    assert rows["d"].watch_checked is None and rows["d"].last_checked is None
    assert rows["s"].watch_fingerprint is None


def test_run_watch_defers_past_the_cap(tmp_path, monkeypatch):
    import conference_agent.refresh as refresh_mod

    monkeypatch.setattr(refresh_mod, "WATCH_MAX_AGENT_PER_RUN", 1)
    url = _db_url(tmp_path)
    _seed_watch_db(url)
    report = run_watch(url, today=TODAY, check=lambda u: _check("fp1"),
                       refresh=lambda targets, attendance_hints=None: [], log=lambda m: None)
    # The daily-tier series goes first; the stale one waits, unstamped.
    assert (report.researched, report.deferred) == (["d"], ["s"])
    assert _rows(url)["s"].watch_checked is None


def test_run_watch_dry_run_writes_nothing(tmp_path):
    url = _db_url(tmp_path)
    _seed_watch_db(url)

    def refresh(targets, attendance_hints=None):
        raise AssertionError("dry run must not research")

    report = run_watch(url, today=TODAY, check=lambda u: _check("fp1"), refresh=refresh,
                       dry_run=True, log=lambda m: None)
    assert [d.id for d in report.decisions if d.run_agent] == ["d", "s"]
    assert all(r.watch_checked is None for r in _rows(url).values())


def test_run_watch_passes_each_targets_attendance_hint(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [Conference(acronym="HNT", name="Hint Test Meeting", subcategory="radiology",
                    url="https://hnt.org", upcoming_abstract_deadline=TODAY + timedelta(days=5),
                    attendance=5000, attendance_year=2025,
                    attendance_source="https://hnt.org/2025-stats")],
        db_url=url,
    )
    passed = []

    def refresh(targets, attendance_hints=None):
        passed.append(attendance_hints)
        return []

    run_watch(url, today=TODAY, check=lambda u: _check("fp"), refresh=refresh, log=lambda m: None)
    assert passed == [
        {"HNT — Hint Test Meeting": {"source": "https://hnt.org/2025-stats", "year": 2025}}
    ]
