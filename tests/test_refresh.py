"""Offline tests for the per-conference auto-check policy (``refresh`` module).

The predicate :func:`is_due_for_check` is pure, so most cases construct a bare
``ConferenceRow`` and pin ``today`` for determinism. A couple of cases exercise
the database-backed selection/marking helpers against a temporary SQLite file.
"""

from datetime import date, timedelta

from conference_agent.database import ConferenceRow, upsert_conferences
from conference_agent.models import Conference
from conference_agent.refresh import (
    _add_months,
    due_subcategories,
    is_due_for_check,
    mark_subcategories_checked,
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


def test_past_one_year_is_not_due():
    # Edition over a year ago -> assume dead or infrequent, stop checking.
    row = _row(prior_start_date=date(2025, 5, 17))
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
