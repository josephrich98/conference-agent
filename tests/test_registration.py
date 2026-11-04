"""Offline tests for the Registration column's line format and its research pass.

Covers the normalizer (label spellings, date spellings, the earlier free text
kept as given), the write paths (a survey keeps stored registration; a manual
edit normalizes it), the registration pass's due policy and storage, the
research matching with the agent call replaced, and
``conference-agent discover --registration``.
"""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from conference_agent import discover
from conference_agent.cli import main
from conference_agent.database import (
    ConferenceRow,
    get_engine,
    merge_records,
    query_conferences,
    record_registration,
    registration_targets,
    set_pins,
    upsert_conferences,
)
from conference_agent.models import (
    Conference,
    is_structured_registration,
    name_id,
    normalize_registration,
)

TODAY = date(2026, 10, 30)


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _conf(name, start=None, **extra):
    return Conference(acronym=name.split()[0], name=name, subcategory="radiology",
                      upcoming_start_date=start, upcoming_end_date=start, **extra)


def test_normalize_registration_canonicalizes_labels_dates_and_order():
    text = ("Refund: through 2027-04-01\nNotes:  Members   register first\n"
            "Registration opens: January 2027\nEarly-bird deadline: Mar. 1st, 2027")
    assert normalize_registration(text) == (
        "Opens: 2027-01\nEarly bird deadline: 2027-03-01\n"
        "Refund deadline: 2027-04-01\nNotes: Members register first"
    )
    # One CSV cell, items separated by "; ", and a weekday before the date.
    assert normalize_registration("Opens: Wednesday, July 1, 2026; Final deadline: 5 Sept 2026") == (
        "Opens: 2026-07-01\nFinal deadline: 2026-09-05"
    )
    # A dict (JSON input or the agent's fields); blank items are dropped.
    assert normalize_registration({"opens": "", "final_deadline": "Nov 24, 2026", "notes": "TBA"}) == (
        "Final deadline: 2026-11-24"
    )
    # A value that is not a single date is kept as written.
    assert normalize_registration("Opens: Fall 2026") == "Opens: Fall 2026"
    assert normalize_registration(" \n ") is None
    assert normalize_registration(None) is None


def test_earlier_free_text_is_kept_as_given():
    legacy = "Early bird: through Aug 10, 2026; Advance: through Sep 21"
    assert normalize_registration(legacy) == legacy
    assert normalize_registration("Opened late July 2026; rates rose on Oct 2") == (
        "Opened late July 2026; rates rose on Oct 2"
    )
    assert not is_structured_registration(legacy)
    assert is_structured_registration("Opens: 2026-07-01")
    assert Conference(name="X Meeting", acronym="X", subcategory="a",
                      upcoming_registration="Early bird: 2026-08-10").upcoming_registration == (
        "Early bird deadline: 2026-08-10"
    )


def test_survey_keeps_stored_registration_and_manual_edit_normalizes(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf("Alpha Meeting", upcoming_registration="Opens: 2026-07-01")], db_url=url)
    # A survey does not research registration, so its record carries none.
    upsert_conferences([_conf("Alpha Meeting", TODAY)], db_url=url)
    (conf,) = query_conferences(db_url=url)
    assert conf.upcoming_registration == "Opens: 2026-07-01"
    merge_records([{"name": "Alpha Meeting",
                    "upcoming_registration": {"opens": "Jul 1, 2026", "refund_deadline": "Oct 1, 2026"}}],
                  db_url=url)
    (conf,) = query_conferences(db_url=url)
    assert conf.upcoming_registration == "Opens: 2026-07-01\nRefund deadline: 2026-10-01"


def test_targets_need_a_date_ahead_and_follow_the_recheck_policy(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf("Past Meeting", TODAY - timedelta(days=1)),
            _conf("Soon Meeting", TODAY + timedelta(days=60)),
            _conf("Later Meeting", TODAY + timedelta(days=90),
                  upcoming_registration="Early bird: through Aug 10; Regular: after"),
            _conf("Undated Meeting"),
        ],
        db_url=url,
    )
    # Never researched: due, soonest meeting first.
    assert [c.name for c in registration_targets(url, TODAY)] == ["Soon Meeting", "Later Meeting"]

    record_registration({name_id("Soon Meeting"): "Opens: 2026-11-01"},
                        [name_id("Soon Meeting"), name_id("Later Meeting")], db_url=url, today=TODAY)
    assert registration_targets(url, TODAY) == []
    # Still the earlier free text: retried after 14 days. In the line format:
    # re-researched after 45.
    assert [c.name for c in registration_targets(url, TODAY + timedelta(days=14))] == ["Later Meeting"]
    assert [c.name for c in registration_targets(url, TODAY + timedelta(days=45))] == [
        "Soon Meeting", "Later Meeting"]
    assert registration_targets(url, TODAY, force=True)[0].name == "Soon Meeting"
    # A pinned registration is never researched.
    set_pins(name_id("Soon Meeting"), pin=["upcoming_registration"], db_url=url)
    assert [c.name for c in registration_targets(url, TODAY, force=True)] == ["Later Meeting"]


def test_record_registration_keeps_stored_text_on_blank_and_respects_pins(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf("Alpha Meeting", TODAY, upcoming_registration="Opens: 2026-07-01"),
                        _conf("Beta Meeting", TODAY, upcoming_registration="Opens: 2026-07-01")],
                       db_url=url)
    alpha, beta = name_id("Alpha Meeting"), name_id("Beta Meeting")
    set_pins(beta, pin=["upcoming_registration"], db_url=url)
    changed = record_registration({alpha: None, beta: "Opens: 2026-08-01"}, [alpha, beta],
                                  db_url=url, today=TODAY)
    assert changed == 0
    with Session(get_engine(url)) as session:
        for row_id in (alpha, beta):
            row = session.get(ConferenceRow, row_id)
            assert row.upcoming_registration == "Opens: 2026-07-01"
            assert row.registration_checked == TODAY


def test_research_registration_matches_targets_and_formats(monkeypatch):
    targets = [_conf("Alpha Meeting", TODAY), _conf("Beta Meeting", TODAY)]
    parsed = discover._ExtractedRegistrationList(conferences=[
        discover._ExtractedRegistration(name="Alpha Meeting", opens="2026-07-01",
                                        early_bird_deadline="Aug 10, 2026"),
        discover._ExtractedRegistration(name="Beta Mtg", acronym="beta"),
        discover._ExtractedRegistration(name="Unknown", acronym="ZZZ", opens="2026-01-01"),
    ])
    monkeypatch.setattr(discover, "_research_and_extract", lambda *a, **k: parsed)
    found = discover.research_registration(targets)
    assert found == {
        name_id("Alpha Meeting"): "Opens: 2026-07-01\nEarly bird deadline: 2026-08-10",
        name_id("Beta Meeting"): None,
    }


def test_discover_registration_researches_due_series(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    ahead = date.today() + timedelta(days=10)
    upsert_conferences([_conf("Alpha Meeting", ahead), _conf("Beta Meeting")], db_url=url)
    seen = []

    def fake_research(targets, **_):
        seen.append([t.name for t in targets])
        return {t.id: "Final deadline: 2030-01-02" for t in targets}

    monkeypatch.setattr(discover, "research_registration", fake_research)
    assert main(["--db", url, "discover", "--registration"]) == 0
    assert seen == [["Alpha Meeting"]]
    by_name = {c.name: c for c in query_conferences(db_url=url)}
    assert by_name["Alpha Meeting"].upcoming_registration == "Final deadline: 2030-01-02"
    # Researched today, so a second run has nothing due.
    assert main(["--db", url, "discover", "--registration"]) == 0
    assert len(seen) == 1
