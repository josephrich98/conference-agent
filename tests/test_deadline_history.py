"""Past deadline history and the Upcoming / Past deadline split.

Each deadline kind keeps a history column (``*_deadline_history``: ISO dates,
most recent first) of the deadlines of editions no longer in the prior /
upcoming slots. The table's Past columns list that history plus the prior
edition's deadline plus an upcoming one that has passed; the Upcoming columns
show only a deadline still ahead.
"""

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

import conference_agent.database as db
import conference_agent.web.app as appmod
from conference_agent.cli import add_field_schema, main
from conference_agent.database import (
    ConferenceRow,
    format_deadline_history,
    get_engine,
    merge_records,
    parse_deadline_history,
    parse_past_deadlines,
    past_deadlines,
    roll_past_editions,
    upcoming_deadline,
    upsert_conferences,
)
from conference_agent.models import Conference

_STATIC = Path(__file__).parent.parent / "conference_agent" / "web" / "static"
_TODAY = date(2026, 11, 12)


def _url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _row(url, row_id="history-test-conference"):
    with Session(get_engine(url)) as session:
        row = session.get(ConferenceRow, row_id)
        session.expunge(row)
        return row


def _base(**fields):
    record = {"name": "History Test Conference", "acronym": "HTC", "subcategory": "genomics"}
    record.update(fields)
    return record


def test_parse_and_format_history():
    assert parse_deadline_history("2023-03-01\n2025-03-02\n2023-03-01\nnot a date") == [
        date(2025, 3, 2), date(2023, 3, 1),
    ]
    assert parse_deadline_history(None) == []
    assert format_deadline_history([date(2023, 3, 1), date(2025, 3, 2)]) == "2025-03-02\n2023-03-01"
    assert format_deadline_history([]) is None


def test_upcoming_and_past_split():
    row = {
        "upcoming_abstract_deadline": "2026-11-11",
        "prior_abstract_deadline": "2025-11-10",
        "abstract_deadline_history": "2024-11-09\n2025-11-10",
        "upcoming_paper_deadline": "2026-11-12",
    }
    # Yesterday's upcoming deadline has passed; today's has not.
    assert upcoming_deadline(row, "abstract", _TODAY) is None
    assert past_deadlines(row, "abstract", _TODAY) == [
        date(2026, 11, 11), date(2025, 11, 10), date(2024, 11, 9),
    ]
    assert upcoming_deadline(row, "paper", _TODAY) == date(2026, 11, 12)
    assert past_deadlines(row, "paper", _TODAY) == []


def test_roll_archives_the_replaced_prior_edition(tmp_path):
    url = _url(tmp_path)
    merge_records([_base(
        prior_abstract_deadline="2024-03-01", prior_start_date="2024-06-01",
        upcoming_abstract_deadline="2025-03-01", upcoming_start_date="2025-06-01",
        upcoming_end_date="2025-06-03",
    )], db_url=url)
    assert roll_past_editions(db_url=url, today=_TODAY)["rolled"] == ["history-test-conference"]
    row = _row(url)
    assert row.prior_abstract_deadline == date(2025, 3, 1)
    assert row.abstract_deadline_history == "2024-03-01"
    assert past_deadlines(row, "abstract", _TODAY) == [date(2025, 3, 1), date(2024, 3, 1)]
    # Rolling again changes nothing (no duplicate entries).
    roll_past_editions(db_url=url, today=_TODAY)
    assert _row(url).abstract_deadline_history == "2024-03-01"


def test_correction_within_an_edition_is_not_archived(tmp_path):
    url = _url(tmp_path)
    merge_records([_base(prior_abstract_deadline="2025-03-01", prior_start_date="2025-06-01")], db_url=url)
    # The same edition's deadline was wrong: the old date is dropped, not kept.
    merge_records([_base(prior_abstract_deadline="2025-03-05")], db_url=url)
    row = _row(url)
    assert row.prior_abstract_deadline == date(2025, 3, 5)
    assert row.abstract_deadline_history is None


def test_discovery_reporting_a_new_edition_archives_the_old_prior(tmp_path):
    url = _url(tmp_path)

    def conf(**dates):
        return Conference(name="History Test Conference", acronym="HTC", subcategory="genomics", **dates)

    upsert_conferences([conf(
        prior_paper_deadline=date(2024, 1, 10), prior_start_date=date(2024, 5, 1),
        upcoming_paper_deadline=date(2025, 1, 12), upcoming_start_date=date(2025, 5, 2),
    )], db_url=url)
    upsert_conferences([conf(
        prior_paper_deadline=date(2025, 1, 12), prior_start_date=date(2025, 5, 2),
        upcoming_paper_deadline=date(2027, 1, 11), upcoming_start_date=date(2027, 5, 3),
    )], db_url=url)
    row = _row(url)
    assert row.paper_deadline_history == "2024-01-10"


def test_entered_past_deadlines_and_csv_history_columns(tmp_path):
    url = _url(tmp_path)
    merge_records([_base(
        prior_abstract_deadline="2025-03-01",
        past_deadlines=[("abstract", date(2023, 3, 2)), ("paper", date(2022, 5, 1))],
    )], db_url=url)
    # A CSV export's history column re-imports (merged, deduplicated).
    merge_records([_base(abstract_deadline_history="2023-03-02\n2021-03-03")], db_url=url)
    row = _row(url)
    assert row.abstract_deadline_history == "2023-03-02\n2021-03-03"
    assert row.paper_deadline_history == "2022-05-01"


def test_parse_past_deadlines_shapes_and_errors():
    expected = [("abstract", date(2024, 3, 1)), ("late_abstract", date(2024, 5, 1))]
    assert parse_past_deadlines(
        [{"type": "abstract", "date": "2024-03-01"}, {"type": "late abstract", "date": "2024-05-01"}]
    ) == expected
    assert parse_past_deadlines([["abstract", "2024-03-01"], ["late_abstract", "2024-05-01"]]) == expected
    assert parse_past_deadlines("abstract 2024-03-01; late abstract 2024-05-01") == expected
    for bad in ([{"type": "poster", "date": "2024-03-01"}], [{"type": "paper", "date": "03/01/2024"}],
                [{"type": "paper"}]):
        with pytest.raises(ValueError):
            parse_past_deadlines(bad)


def test_cli_add_flags_json_and_future_dates(tmp_path, capsys):
    url = _url(tmp_path)
    assert main([
        "--db", url, "add", "--conference-name", "History Test Conference",
        "--subcategory", "genomics",
        "--past-deadlines", "abstract", "2024-03-01",
        "--past-deadlines", "abstract", "2023-03-02",
    ]) == 0
    assert _row(url).abstract_deadline_history == "2024-03-01\n2023-03-02"

    path = tmp_path / "edit.json"
    path.write_text(json.dumps({
        "conference_name": "History Test Conference",
        "past_deadlines": [{"type": "paper", "date": "2024-06-01"}],
    }), encoding="utf-8")
    assert main(["--db", url, "add", "--update", "--json", str(path)]) == 0
    assert _row(url).paper_deadline_history == "2024-06-01"

    code = main([
        "--db", url, "add", "--update", "--conference-name", "History Test Conference",
        "--past-deadlines", "paper", "2999-01-01",
    ])
    assert code == 1
    assert "after today" in capsys.readouterr().err


def test_schema_exposes_past_deadlines_field():
    kinds = {f["name"]: f["kind"] for f in add_field_schema()["fields"]}
    assert kinds["past_deadlines"] == "past_deadlines"


def test_old_database_gains_the_history_columns(tmp_path):
    url = _url(tmp_path)
    engine = get_engine(url)
    with engine.begin() as conn:
        for kind in ("abstract", "late_abstract", "paper"):
            conn.execute(text(f"ALTER TABLE conferences DROP COLUMN {kind}_deadline_history"))
    engine.dispose()
    db._ENGINES.pop(url, None)
    merge_records([_base(past_deadlines=[("abstract", date(2024, 3, 1))])], db_url=url)
    assert _row(url).abstract_deadline_history == "2024-03-01"


def test_api_sorts_past_columns_by_most_recent(tmp_path, monkeypatch):
    url = _url(tmp_path)
    monkeypatch.setenv("CONFERENCE_DATABASE_URL", url)
    merge_records([
        {"name": "Alpha Meeting", "acronym": "A", "subcategory": "x",
         "prior_abstract_deadline": "2024-01-01", "abstract_deadline_history": "2023-01-01"},
        {"name": "Beta Meeting", "acronym": "B", "subcategory": "x",
         "abstract_deadline_history": "2025-06-01\n2020-01-01"},
        {"name": "Gamma Meeting", "acronym": "C", "subcategory": "x",
         "upcoming_abstract_deadline": "2999-01-01"},
    ], db_url=url)
    rows = appmod._run_search("", "past_abstract_deadline", "desc")
    assert [r.acronym for r in rows] == ["B", "A", "C"]  # no past date sorts last
    rows = appmod._run_search("", "upcoming_abstract_deadline", "asc")
    assert [r.acronym for r in rows][0] == "C"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_split_uses_the_deadline_instant():
    # 2026-11-12 23:59 ET closes at 2026-11-13 04:59 UTC.
    rows = [{
        "id": "x", "acronym": "X",
        "upcoming_abstract_deadline": "2026-11-12", "abstract_time": "23:59", "abstract_timezone": "ET",
        "prior_abstract_deadline": "2025-11-10", "abstract_deadline_history": "2024-11-09",
    }]
    before = "Date.UTC(2026, 10, 13, 4, 30)"
    after = "Date.UTC(2026, 10, 13, 5, 30)"
    script = (
        f"global.ConferenceCalendar = require({json.dumps(str(_STATIC / 'calendar.js'))});"
        f"const S = require({json.dumps(str(_STATIC / 'search.js'))});"
        f"const r = {json.dumps(rows)}[0];"
        f"const out = [{before}, {after}].map((now) => "
        "[S.upcomingDeadline(r, 'abstract', now), S.pastDeadlines(r, 'abstract', now)]);"
        "process.stdout.write(JSON.stringify(out));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [
        ["2026-11-12", ["2025-11-10", "2024-11-09"]],
        [None, ["2026-11-12", "2025-11-10", "2024-11-09"]],
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_sort_on_past_column():
    rows = [
        {"id": "a", "acronym": "A", "prior_abstract_deadline": "2024-01-01",
         "abstract_deadline_history": "2023-01-01"},
        {"id": "b", "acronym": "B", "abstract_deadline_history": "2025-06-01\n2020-01-01"},
        {"id": "c", "acronym": "C", "upcoming_abstract_deadline": "2999-01-01"},
    ]
    script = (
        f"const {{ sortRows }} = require({json.dumps(str(_STATIC / 'search.js'))});"
        f"const rows = {json.dumps(rows)};"
        "process.stdout.write(JSON.stringify(["
        "sortRows(rows, 'past_abstract_deadline', 'desc').map((r) => r.id),"
        "sortRows(rows, 'upcoming_abstract_deadline', 'asc').map((r) => r.id)]));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [["b", "a", "c"], ["c", "a", "b"]]


def test_conference_page_lists_earlier_deadlines():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from seo_pages import conference_page

    _, doc = conference_page({
        "id": "hist", "acronym": "HIST", "name": "History Meeting",
        "prior_abstract_deadline": "2025-03-01",
        "abstract_deadline_history": "2025-03-01\n2024-03-02",
    }, "2026-11-12")
    assert "<h2>Earlier editions</h2>" in doc
    assert "March 2, 2024" in doc
    # The prior edition's date is shown under Prior edition, not repeated.
    assert doc.count("March 1, 2025") == 1
