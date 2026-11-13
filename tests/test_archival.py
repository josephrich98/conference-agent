"""Offline tests for the per-series ``archival`` yes/no field.

Covers the model normalizer, the additive column migration on open, the
keep-on-blank discovery upsert and re-check, ``add`` (flags, CSV, JSON, and
validation), pinning, the serialized row, the search, and the sort (API and,
when ``node`` is available, the static site's ``search.js``).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import conference_agent.web.app as appmod
from conference_agent.cli import add_field_schema, main
from conference_agent.database import (
    ConferenceRow,
    apply_refreshed_conferences,
    get_engine,
    merge_records,
    query_conferences,
    set_pins,
    upsert_conferences,
)
from conference_agent.discover import _ExtractedConference, _to_conference
from conference_agent.models import Conference, normalize_archival
from conference_agent.web.search import QueryError, build_filter, row_to_dict

_SEARCH_JS = Path(__file__).parent.parent / "conference_agent" / "web" / "static" / "search.js"
_NAME = "International Conference on Machine Learning"


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _icml(**extra):
    return Conference(acronym="ICML", name=_NAME, subcategory="machine learning", **extra)


def _stored(url, acronym="ICML"):
    return next(c for c in query_conferences(db_url=url) if c.acronym == acronym)


# --- Model normalization ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("yes", True), ("Yes", True), ("y", True), ("true", True), ("TRUE", True), ("1", True),
        (1, True), (True, True), ("archival", True),
        ("no", False), ("NO", False), ("n", False), ("false", False), ("0", False), (0, False),
        (False, False), ("non-archival", False),
        ("", None), ("  ", None), (None, None), ("unknown", None),
    ],
)  # fmt: skip
def test_normalize_archival(value, expected):
    assert normalize_archival(value) is expected


@pytest.mark.parametrize("value", ["maybe", "2", "sometimes"])
def test_invalid_archival_raises(value):
    with pytest.raises(ValueError):
        normalize_archival(value)
    with pytest.raises(ValueError):
        _icml(archival=value)


def test_model_defaults_to_unknown():
    assert _icml().archival is None
    assert _icml(archival="yes").archival is True
    assert _icml(archival="0").archival is False


# --- Migration ----------------------------------------------------------------


def test_migration_adds_the_column_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    url = f"sqlite:///{path}"
    upsert_conferences([_icml()], db_url=url, manual=True)
    # Simulate a database created before the column existed.
    con = sqlite3.connect(path)
    con.execute("DROP INDEX IF EXISTS ix_conferences_archival")
    con.execute("ALTER TABLE conferences DROP COLUMN archival")
    con.commit()
    assert "archival" not in {r[1] for r in con.execute("PRAGMA table_info(conferences)")}
    con.close()

    from conference_agent import database

    database._ENGINES.clear()
    assert _stored(url).archival is None  # opening re-adds the column, as NULL
    con = sqlite3.connect(path)
    assert "archival" in {r[1] for r in con.execute("PRAGMA table_info(conferences)")}
    con.close()
    merge_records([{"name": _NAME, "archival": "yes"}], db_url=url)
    assert _stored(url).archival is True


# --- Writes -------------------------------------------------------------------


def test_survey_that_reports_none_keeps_the_stored_value(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_icml(archival=False)], db_url=url, manual=True)
    upsert_conferences([_icml(attendance=9000)], db_url=url)
    assert _stored(url).archival is False
    # A survey that does report an answer updates it.
    upsert_conferences([_icml(archival=True)], db_url=url)
    assert _stored(url).archival is True
    # A manual overwrite replaces the whole row, unknown included.
    upsert_conferences([_icml()], db_url=url, manual=True)
    assert _stored(url).archival is None


def test_recheck_keeps_or_updates_the_value(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_icml(archival=True)], db_url=url, manual=True)
    apply_refreshed_conferences([_icml()], db_url=url)
    assert _stored(url).archival is True
    apply_refreshed_conferences([_icml(archival=False)], db_url=url)
    assert _stored(url).archival is False


def test_merge_records_explicit_no_is_a_value_and_blank_keeps(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_icml(archival=True)], db_url=url, manual=True)
    merge_records([{"name": _NAME, "archival": ""}], db_url=url)
    assert _stored(url).archival is True
    merge_records([{"name": _NAME, "archival": "maybe"}], db_url=url)
    assert _stored(url).archival is True
    merge_records([{"name": _NAME, "archival": False}], db_url=url)
    assert _stored(url).archival is False


def test_archival_is_pinnable(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_icml(archival=True)], db_url=url, manual=True)
    assert set_pins(_icml().id, pin=["archival"], db_url=url) == ["archival"]
    upsert_conferences([_icml(archival=False)], db_url=url)
    assert _stored(url).archival is True
    apply_refreshed_conferences([_icml(archival=False)], db_url=url)
    assert _stored(url).archival is True


# --- add: flags, CSV, JSON, validation ----------------------------------------


def test_add_flag_csv_json_round_trip(tmp_path):
    url = _db_url(tmp_path)
    assert main([
        "--db", url, "add", "--conference-acronym", "ICML", "--conference-name", _NAME,
        "--subcategory", "machine learning", "--archival", "true",
    ]) == 0  # fmt: skip
    assert _stored(url).archival is True

    csv_path = tmp_path / "in.csv"
    csv_path.write_text(f"conference_name,archival\n{_NAME},no\n")
    assert main(["--db", url, "add", "--update", "--csv", str(csv_path)]) == 0
    assert _stored(url).archival is False

    json_path = tmp_path / "in.json"
    json_path.write_text(json.dumps([{"conference_name": _NAME, "archival": True}]))
    assert main(["--db", url, "add", "--update", "--json", str(json_path)]) == 0
    assert _stored(url).archival is True

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"conference_name": _NAME, "archival": "sometimes"}]))
    assert main(["--db", url, "add", "--update", "--json", str(bad)]) == 1
    assert _stored(url).archival is True


def test_info_json_round_trips_through_add(tmp_path, capsys):
    url = _db_url(tmp_path)
    upsert_conferences([_icml(archival=False)], db_url=url, manual=True)
    capsys.readouterr()
    assert main(["--db", url, "info", "ICML", "--json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["archival"] is False

    other = f"sqlite:///{tmp_path / 'other.db'}"
    path = tmp_path / "row.json"
    path.write_text(json.dumps([record]))
    assert main(["--db", other, "add", "--json", str(path)]) == 0
    assert _stored(other).archival is False


def test_add_field_schema_lists_a_yes_no_choice():
    field = next(f for f in add_field_schema()["fields"] if f["name"] == "archival")
    assert field["kind"] == "enum" and field["choices"] == ["yes", "no"]
    assert field["website"] == "own column"


# --- Discovery extraction -----------------------------------------------------


def _extracted(**extra):
    blank = {name: "" for name in _ExtractedConference.model_fields}
    blank.update(acronym="ICML", name=_NAME, subcategory="machine learning")
    blank.update(extra)
    return _ExtractedConference(**blank)


@pytest.mark.parametrize(("value", "expected"), [("yes", True), ("No", False), ("", None), ("unclear", None)])
def test_discovery_extraction_parses_archival(value, expected):
    assert _to_conference(_extracted(archival=value)).archival is expected


# --- Serialized row, search, and sort -----------------------------------------


_ROWS = {"YES": True, "NO": False, "UNK": None}


def _seed(url):
    upsert_conferences(
        [
            Conference(acronym=k, name=f"{k} meeting", subcategory="machine learning", archival=v)
            for k, v in _ROWS.items()
        ],
        db_url=url,
        manual=True,
    )


def _matching(url, query):
    from sqlalchemy import select

    with Session(get_engine(url)) as session:
        stmt = select(ConferenceRow.acronym).where(build_filter(query))
        return sorted(session.scalars(stmt))


def test_row_to_dict_and_search(tmp_path):
    url = _db_url(tmp_path)
    _seed(url)
    with Session(get_engine(url)) as session:
        rows = {r.acronym: row_to_dict(r) for r in session.query(ConferenceRow)}
    assert {k: r["archival"] for k, r in rows.items()} == _ROWS

    assert _matching(url, "archival:yes") == ["YES"]
    assert _matching(url, "archival:true") == ["YES"]
    assert _matching(url, "archival:no") == ["NO"]
    assert _matching(url, "archival:*") == ["NO", "YES"]
    assert _matching(url, "NOT archival:*") == ["UNK"]
    # An unknown answer matches neither value, so NOT keeps it.
    assert _matching(url, "NOT archival:yes") == ["NO", "UNK"]
    with pytest.raises(QueryError):
        build_filter("archival:maybe")
    with pytest.raises(QueryError):
        build_filter("archival>yes")


_SORT_CASES = [("asc", ["YES", "NO", "UNK"]), ("desc", ["NO", "YES", "UNK"])]


def test_api_archival_sort(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    monkeypatch.setenv("CONFERENCE_DATABASE_URL", url)
    _seed(url)
    for order, expected in _SORT_CASES:
        assert [r.acronym for r in appmod._run_search("", "archival", order)] == expected, order


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_archival_sort():
    rows = [{"id": k, "acronym": k, "archival": v} for k, v in _ROWS.items()]
    script = (
        f"const {{ sortRows }} = require({json.dumps(str(_SEARCH_JS))});"
        f"const rows = {json.dumps(rows)};"
        f"const orders = {json.dumps([o for o, _ in _SORT_CASES])};"
        "const out = orders.map((o) => sortRows(rows, 'archival', o).map((r) => r.id));"
        "process.stdout.write(JSON.stringify(out));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [expected for _, expected in _SORT_CASES]
