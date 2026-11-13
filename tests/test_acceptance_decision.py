"""Acceptance rates and the acceptance decision date.

Acceptance rates are per-series lines ("YEAR | ACCEPTED/SUBMITTED | SOURCE" or
"YEAR | RATE% | SOURCE"), with the most recent year's rate derived into
``acceptance_rate``. The decision date is a per-edition date with Upcoming /
Past columns backed by ``decision_deadline_history``, like the deadlines, but
with no time, extensions, month, or reminder.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.calendar_sync import conferences_to_ics
from conference_agent.cli import add_field_schema
from conference_agent.database import (
    HISTORY_COLUMNS,
    ConferenceRow,
    get_engine,
    merge_records,
    parse_past_deadlines,
    past_deadlines,
    roll_past_editions,
    upcoming_deadline,
    upsert_conferences,
)
from conference_agent.models import (
    Conference,
    merge_acceptance_rates,
    normalize_acceptance_rates,
    parse_acceptance_rates,
)
from conference_agent.web.search import build_filter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fill_ccfddl  # noqa: E402


def _url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _row(url, row_id="acceptance-test-conference"):
    with Session(get_engine(url)) as session:
        row = session.get(ConferenceRow, row_id)
        session.expunge(row)
        return row


def _base(**fields):
    record = {"name": "Acceptance Test Conference", "acronym": "ATC", "subcategory": "machine learning"}
    record.update(fields)
    return record


# --- acceptance rates: parsing ---------------------------------------------


def test_parse_lines_counts_and_percent():
    entries = parse_acceptance_rates("2024 | 25%\n2026 | 4,167/23,680 | https://x.org\n2025: 3032/12957")
    assert [e["year"] for e in entries] == [2026, 2025, 2024]
    assert entries[0] == {
        "year": 2026, "accepted": 4167, "submitted": 23680, "rate": 17.6, "source": "https://x.org",
    }
    assert entries[2]["rate"] == 25.0 and entries[2]["accepted"] is None


def test_parse_ccfddl_dicts_fraction_rate():
    entries = parse_acceptance_rates([{"year": 2025, "rate": 0.234, "source": "s"}])
    assert entries[0]["rate"] == 23.4


def test_normalize_is_canonical_and_semicolon_separated():
    assert normalize_acceptance_rates("2023 | 1/4; 2024: 30 %") == "2024 | 30%\n2023 | 1/4"
    assert normalize_acceptance_rates("") is None


def test_first_entry_for_a_year_wins_and_merge_replaces_years():
    assert parse_acceptance_rates("2024 | 30%\n2024 | 20%")[0]["rate"] == 30.0
    merged = merge_acceptance_rates("2024 | 25%\n2023 | 1/4", "2024 | 1/2 | src")
    assert merged == "2024 | 1/2 | src\n2023 | 1/4"


@pytest.mark.parametrize(
    "bad",
    ["2024 | 5/4", "2024 | 120%", "1850 | 10%", "2024 | lots", "nonsense", "2024 | 0/0",
     [{"year": 2024, "accepted": 3}]],
)
def test_parse_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        parse_acceptance_rates(bad)


def test_model_properties():
    conf = Conference(**_base(acceptance="2026 | 4167/23680\n2025 | 23.4%"))
    assert conf.acceptance_rate == 17.6
    assert conf.acceptance_display == "17.6% (2026)"
    assert Conference(**_base()).acceptance_display is None


# --- acceptance rates: storage and search -----------------------------------


def test_merge_records_derives_rate_and_keeps_other_years(tmp_path):
    url = _url(tmp_path)
    merge_records([_base(acceptance_rates="2024 | 25%\n2023 | 1/4")], db_url=url)
    assert _row(url).acceptance_rate == 25.0
    merge_records([{"id": "acceptance-test-conference",
                    "acceptance_rates": [{"year": 2025, "accepted": 1, "submitted": 5}]}], db_url=url)
    row = _row(url)
    assert row.acceptance_rates == "2025 | 1/5\n2024 | 25%\n2023 | 1/4"
    assert row.acceptance_rate == 20.0


def test_discovery_keeps_stored_acceptance_rates(tmp_path):
    url = _url(tmp_path)
    merge_records([_base(acceptance_rates="2025 | 1/5")], db_url=url)
    upsert_conferences([Conference(**_base(url="https://atc.org"))], db_url=url)
    row = _row(url)
    assert row.acceptance_rates == "2025 | 1/5" and row.acceptance_rate == 20.0


def test_acceptance_search(tmp_path):
    url = _url(tmp_path)
    merge_records([_base(acceptance_rates="2025 | 1/5")], db_url=url)
    with Session(get_engine(url)) as session:
        def ids(query):
            return list(session.scalars(select(ConferenceRow.id).where(build_filter(query))))
        assert ids("acceptance<25") == ["acceptance-test-conference"]
        assert ids("acceptance>=20%") == ["acceptance-test-conference"]
        assert ids("acceptance>20") == []


def test_add_schema_lists_the_new_fields():
    names = {f["name"]: f for f in add_field_schema()["fields"]}
    assert names["acceptance_rates"]["kind"] == "text"
    assert names["decision_due"]["stored_as"] == "upcoming_decision_deadline"
    assert names["prior_decision_due"]["stored_as"] == "prior_decision_deadline"


# --- decision date ----------------------------------------------------------


def test_decision_alias_and_upcoming_past_split():
    conf = Conference(**_base(upcoming_decision_date="2026-12-16", prior_decision_date="2025-12-10"))
    assert conf.upcoming_decision_deadline == date(2026, 12, 16)
    row = {
        "upcoming_decision_deadline": "2026-12-16",
        "prior_decision_deadline": "2025-12-10",
        "decision_deadline_history": "2024-12-01",
    }
    today = date(2026, 11, 13)
    assert upcoming_deadline(row, "decision", today) == date(2026, 12, 16)
    assert past_deadlines(row, "decision", today) == [date(2025, 12, 10), date(2024, 12, 1)]
    later = date(2026, 12, 17)
    assert upcoming_deadline(row, "decision", later) is None
    assert past_deadlines(row, "decision", later)[0] == date(2026, 12, 16)


def test_past_decision_entries_and_history_column():
    assert "decision_deadline_history" in HISTORY_COLUMNS
    assert parse_past_deadlines("decision 2024-12-01; notification date 2023-12-02") == [
        ("decision", date(2024, 12, 1)), ("decision", date(2023, 12, 2)),
    ]


def test_roll_archives_the_replaced_decision_date(tmp_path):
    url = _url(tmp_path)
    today = date(2026, 11, 13)
    merge_records([_base(
        prior_start_date="2025-07-10", prior_decision_deadline="2025-03-01",
        upcoming_start_date="2026-07-10", upcoming_end_date="2026-07-14",
        upcoming_decision_deadline="2026-03-02",
    )], db_url=url)
    roll_past_editions(db_url=url, today=today)
    row = _row(url)
    assert row.prior_decision_deadline == date(2026, 3, 2)
    assert row.upcoming_decision_deadline is None
    assert row.decision_deadline_history == "2025-03-01"


def test_decision_calendar_event_has_no_reminder():
    conf = Conference(**_base(upcoming_decision_deadline="2026-12-16", upcoming_start_date="2027-04-26"))
    ics = conferences_to_ics([conf])
    events = ics.split("BEGIN:VEVENT")[1:]
    decision = [e for e in events if "acceptance decisions" in e]
    assert len(decision) == 1
    assert "DTSTART;VALUE=DATE:20261216" in decision[0]
    assert "VALARM" not in decision[0]
    assert all("VALARM" in e for e in events if e is not decision[0])


# --- ccfddl fill / cross-check -----------------------------------------------


def test_same_deadline_compares_instants_across_zones():
    utc = timezone.utc
    # "May 4 23:59 AoE" is listed by ccfddl as "May 5 11:59 UTC".
    assert fill_ccfddl.same_deadline(date(2026, 5, 4), "23:59", "AoE", datetime(2026, 5, 5, 11, 59, 59, tzinfo=utc))
    assert not fill_ccfddl.same_deadline(date(2026, 5, 4), "23:59", "AoE", datetime(2026, 5, 6, 11, 59, tzinfo=utc))
    # No stored time: ccfddl's instant read as a date in the stored zone.
    aoe = timezone(timedelta(hours=-12))
    assert fill_ccfddl.same_deadline(date(2026, 10, 2), None, "AoE", datetime(2026, 10, 2, 23, 59, tzinfo=aoe))
    assert not fill_ccfddl.same_deadline(date(2026, 10, 3), None, "AoE", datetime(2026, 10, 2, 23, 59, tzinfo=aoe))


def test_plan_fills_blank_decision_and_reports_disagreements(tmp_path):
    url = _url(tmp_path)
    merge_records([_base(
        upcoming_start_date="2027-04-26", upcoming_paper_deadline="2026-09-26",
        paper_time="23:59", paper_timezone="AoE",
    )], db_url=url)
    series = {"confs": [{
        "year": 2027, "timezone": "UTC-12",
        "timeline": [{"abstract_deadline": "2026-09-18 23:59:59", "deadline": "2026-09-25 23:59:59",
                      "decision_deadline": "2026-12-16 23:59:59"}, {"deadline": "TBD"}],
    }]}
    fills, notes = fill_ccfddl.plan_row(_row(url), series)
    assert fills == {"upcoming_decision_deadline": date(2026, 12, 16)}
    assert any("paper: stored 2026-09-26" in n for n in notes)
    assert any("abstract: blank here" in n for n in notes)
