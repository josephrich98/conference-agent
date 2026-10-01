"""Tests for structured deadline times (``deadline_time.py`` and its integrations)."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from conference_agent import database as db
from conference_agent.deadline_time import (
    TIMEZONES,
    deadline_entries,
    format_deadline_time,
    format_spec,
    normalize_time,
    normalize_timezone,
    parse_legacy_deadline_time,
)
from conference_agent.models import Conference

_NODE = shutil.which("node")
_RUNNER = Path(__file__).parent / "js" / "run_deadline.js"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("23:59", "23:59"),
        ("9:00", "09:00"),
        ("11:59 PM", "23:59"),
        ("11:59pm", "23:59"),
        ("11:59 p.m.", "23:59"),
        ("12:00 AM", "00:00"),
        ("12 p.m.", "12:00"),
        ("5 PM", "17:00"),
        ("noon", "12:00"),
        ("12:00 PM (Noon)", "12:00"),
        ("23:59:59", "23:59"),
        ("midnight", "23:59"),
        ("25:00", None),
        ("13:00 PM", None),
        ("", None),
        (None, None),
        ("soon", None),
    ],
)
def test_normalize_time(raw, expected):
    assert normalize_time(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("AoE", "AoE"),
        ("anywhere on earth", "AoE"),
        ("UTC-12", "AoE"),
        ("UTC", "UTC"),
        ("UTC+9", "UTC+9"),
        ("GMT-5:30", "UTC-5:30"),
        ("ET", "ET"),
        ("EST", "ET"),
        ("EDT", "ET"),
        ("Eastern Time", "ET"),
        ("CDT", "CT"),
        ("Pacific", "PT"),
        ("CEST", "CET"),
        ("UK time", "UK"),
        ("11:59 PM CDT (UTC-5)", "CT"),  # the main zone wins over the aside
        ("AoE (12:00 noon UTC the following day)", "AoE"),
        ("Asia/Shanghai", "Asia/Shanghai"),
        ("Mars/Olympus", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_timezone(raw, expected):
    assert normalize_timezone(raw) == expected


def test_format_spec_12_and_24_hour():
    assert format_spec("20:00", "AoE") == "8:00 PM AoE"
    assert format_spec("20:00", "AoE", military=True) == "20:00 AoE"
    assert format_spec("00:05", "ET") == "12:05 AM ET"
    assert format_spec("12:00", "ET") == "12:00 PM ET"
    assert format_spec("09:30", None, military=True) == "09:30"
    assert format_spec(None, "UTC") == "UTC"
    assert format_spec(None, None) == ""


_ET = ("20:00", "ET")


def test_identical_times_collapse_to_one_unlabeled_entry():
    specs = {"abstract": _ET, "late_abstract": (None, None), "paper": _ET}
    # The late abstract has no date, so it is not "unknown": nothing to disagree.
    assert deadline_entries(specs, ["abstract", "paper"]) == [(None, _ET)]
    assert format_deadline_time(specs, ["abstract", "paper"]) == "8:00 PM ET"
    assert format_deadline_time(specs, ["abstract", "paper"], military=True) == "20:00 ET"


def test_differing_times_stay_labeled():
    specs = {"abstract": _ET, "late_abstract": ("17:00", "ET"), "paper": ("23:59", "AoE")}
    assert format_deadline_time(specs, ["abstract", "late_abstract", "paper"]) == (
        "abstract: 8:00 PM ET\nlate abstract: 5:00 PM ET\npaper: 11:59 PM AoE"
    )


def test_a_dated_kind_without_a_time_keeps_labels():
    # The paper has a deadline but no recorded time, so claiming "8:00 PM ET" for
    # everything would overstate what was published.
    specs = {"abstract": _ET, "late_abstract": (None, None), "paper": (None, None)}
    assert format_deadline_time(specs, ["abstract", "paper"]) == "abstract: 8:00 PM ET"


def test_single_time_with_one_dated_kind_is_unlabeled():
    specs = {"abstract": _ET, "late_abstract": (None, None), "paper": (None, None)}
    assert format_deadline_time(specs, ["abstract"]) == "8:00 PM ET"
    assert format_deadline_time(specs, []) == "8:00 PM ET"


def test_no_times_gives_none():
    assert format_deadline_time({k: (None, None) for k in ("abstract", "paper")}, ["abstract"]) is None


def test_legacy_text_parses_shared_and_labeled():
    both = ["abstract", "paper"]
    assert parse_legacy_deadline_time("11:59 PM EST", both) == {
        "abstract_time": "23:59",
        "abstract_timezone": "ET",
        "paper_time": "23:59",
        "paper_timezone": "ET",
    }
    # An unlabeled time is not given to a kind the series has no date for.
    assert parse_legacy_deadline_time("AoE") == {
        "abstract_time": "23:59",
        "abstract_timezone": "AoE",
    }
    assert parse_legacy_deadline_time("abstract: 2 p.m. EDT; late abstract: 5 p.m. EDT") == {
        "abstract_time": "14:00",
        "abstract_timezone": "ET",
        "late_abstract_time": "17:00",
        "late_abstract_timezone": "ET",
    }
    # Zone-less time is kept; a bare "EOD" says nothing and is dropped.
    assert parse_legacy_deadline_time("5:00 PM") == {"abstract_time": "17:00"}
    assert parse_legacy_deadline_time("paper: EOD") == {}
    assert parse_legacy_deadline_time(None) == {}


def test_conference_accepts_structured_and_legacy_input():
    conf = Conference(
        acronym="X",
        name="X Conf",
        subcategory="radiology",
        abstract_time="8:00 PM",
        abstract_timezone="Eastern",
        paper_time="20:00",
        paper_timezone="EST",
        upcoming_abstract_deadline=date(2026, 5, 1),
        upcoming_paper_deadline=date(2026, 6, 1),
    )
    assert (conf.abstract_time, conf.abstract_timezone) == ("20:00", "ET")
    assert conf.deadline_time == "8:00 PM ET"

    legacy = Conference(
        acronym="X",
        name="X Conf",
        subcategory="radiology",
        deadline_time="abstract: 5 PM ET; paper: 23:59 AoE",
        abstract_timezone="PT",  # an explicit field wins over the parsed text
    )
    assert (legacy.abstract_time, legacy.abstract_timezone) == ("17:00", "PT")
    assert (legacy.paper_time, legacy.paper_timezone) == ("23:59", "AoE")


# --- database -----------------------------------------------------------------


def _url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 't.db'}"


def test_upsert_stores_columns_and_derived_text(tmp_path):
    url = _url(tmp_path)
    db.upsert_conferences(
        [
            Conference(
                acronym="ABC",
                name="ABC",
                subcategory="radiology",
                abstract_time="20:00",
                abstract_timezone="ET",
                upcoming_abstract_deadline=date(2026, 5, 1),
            )
        ],
        db_url=url,
    )
    with Session(db.get_engine(url)) as s:
        row = s.get(db.ConferenceRow, "abc")
        assert (row.abstract_time, row.abstract_timezone) == ("20:00", "ET")
        assert row.deadline_time == "8:00 PM ET"
    [conf] = [c for c in db.query_conferences(db_url=url) if c.acronym == "ABC"]
    assert conf.abstract_time == "20:00"


def test_merge_records_normalizes_and_rederives(tmp_path):
    url = _url(tmp_path)
    db.upsert_conferences(
        [Conference(acronym="ABC", name="ABC", subcategory="radiology",
                    upcoming_abstract_deadline=date(2026, 5, 1))],
        db_url=url,
    )
    db.merge_records(
        [{"id": "abc", "abstract_time": "11:59 PM", "abstract_timezone": "EST", "paper_time": "bogus"}],
        db_url=url,
    )
    with Session(db.get_engine(url)) as s:
        row = s.get(db.ConferenceRow, "abc")
        assert (row.abstract_time, row.abstract_timezone) == ("23:59", "ET")
        assert row.paper_time is None  # unreadable values are ignored
        assert row.deadline_time == "11:59 PM ET"

    # The legacy free-text key still merges like a newer record (it replaces the
    # stored value), but an explicit structured field in the same record wins.
    db.merge_records(
        [{"id": "abc", "deadline_time": "5 PM PT", "abstract_timezone": "CT"}], db_url=url
    )
    with Session(db.get_engine(url)) as s:
        row = s.get(db.ConferenceRow, "abc")
        assert (row.abstract_time, row.abstract_timezone) == ("17:00", "CT")
        assert row.deadline_time == "5:00 PM CT"


def test_legacy_database_is_migrated_on_open(tmp_path):
    # A database from before the structured columns: only free-text deadline_time.
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE conferences (id TEXT PRIMARY KEY, acronym TEXT NOT NULL, name TEXT NOT NULL,"
            " subcategory TEXT NOT NULL, category TEXT, upcoming_abstract_deadline DATE,"
            " upcoming_paper_deadline DATE, deadline_time TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO conferences VALUES ('A','A','A','radiology','medicine','2026-05-01','2026-06-01','11:59 PM EST'),"
            " ('B','B','B','radiology','medicine','2026-05-01',NULL,'abstract: 5 PM CT; paper: 23:59 AoE'),"
            " ('C','C','C','radiology','medicine',NULL,NULL,'EOD'),"
            " ('D','D','D','radiology','medicine',NULL,NULL,NULL)"
        ))
    engine.dispose()
    url = f"sqlite:///{path}"

    with Session(db.get_engine(url)) as s:
        rows = {r.id: r for r in s.scalars(select(db.ConferenceRow))}
    # The legacy acronym ids also move to name ids on open.
    a = rows["a"]
    assert (a.abstract_time, a.abstract_timezone, a.paper_time, a.paper_timezone) == (
        "23:59", "ET", "23:59", "ET",
    )
    assert a.deadline_time == "11:59 PM ET"  # normalized and collapsed
    b = rows["b"]
    assert (b.abstract_time, b.abstract_timezone, b.late_abstract_time) == ("17:00", "CT", None)
    assert (b.paper_time, b.paper_timezone) == ("23:59", "AoE")
    assert b.deadline_time == "abstract: 5:00 PM CT\npaper: 11:59 PM AoE"
    assert rows["c"].deadline_time == "EOD"  # nothing parseable: left as it was
    assert rows["d"].deadline_time is None


def test_backfill_is_idempotent_and_never_overwrites(tmp_path):
    url = _url(tmp_path)
    db.upsert_conferences(
        [Conference(acronym="ABC", name="ABC", subcategory="radiology",
                    abstract_time="20:00", abstract_timezone="ET",
                    upcoming_abstract_deadline=date(2026, 5, 1))],
        db_url=url,
    )
    assert db.backfill_deadline_times(url) == 0
    assert db.recompute_deadline_times(url) == 0


# --- JS parity ----------------------------------------------------------------


def _node(req: dict) -> dict:
    out = subprocess.run(
        [_NODE, str(_RUNNER)], input=json.dumps(req), capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_js_zone_table_matches_python():
    js = _node({"zones": True})["zones"]
    assert js == {
        code: ({"zone": s["iana"]} if "iana" in s else {"offset": s["offset"]})
        for code, s in TIMEZONES.items()
    }


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_js_deadline_text_matches_python():
    rows = [
        # shared time, one date missing
        {"abstract_time": "20:00", "abstract_timezone": "ET", "paper_time": "20:00",
         "paper_timezone": "ET", "upcoming_abstract_deadline": "2026-05-01",
         "upcoming_paper_deadline": "2026-06-01"},
        # differing
        {"abstract_time": "20:00", "abstract_timezone": "ET", "late_abstract_time": "17:00",
         "late_abstract_timezone": "ET", "paper_time": "23:59", "paper_timezone": "AoE"},
        # paper dated but untimed
        {"abstract_time": "20:00", "abstract_timezone": "ET",
         "prior_paper_deadline": "2025-06-01"},
        # zone only
        {"abstract_timezone": "AoE", "upcoming_abstract_deadline": "2026-05-01"},
        {},
    ]
    got = _node({"rows": rows})["rows"]
    for row, (twelve, military) in zip(rows, got):
        conf = Conference(
            acronym="X",
            name="X",
            subcategory="radiology",
            **{k: v for k, v in row.items() if not k.endswith("deadline")},
            **{
                k: date.fromisoformat(v)
                for k, v in row.items()
                if k.endswith("deadline")
            },
        )
        dated = [
            k
            for k in ("abstract", "late_abstract", "paper")
            if getattr(conf, f"upcoming_{k}_deadline") or getattr(conf, f"prior_{k}_deadline")
        ]
        assert twelve == (conf.deadline_time or "")
        assert military == (format_deadline_time(conf.deadline_specs, dated, military=True) or "")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_js_deadline_instants():
    # [date, time, zone] -> the instant (UTC ms) the deadline has passed.
    cases = [
        # 23:59 ET on a summer date passes at 00:00 EDT (04:00 UTC) the next day.
        (("2026-07-15", "23:59", "ET"), "2026-07-16T04:00:00Z"),
        # The same time in winter (EST, UTC-5).
        (("2026-01-15", "23:59", "ET"), "2026-01-16T05:00:00Z"),
        (("2026-07-15", "23:59", "AoE"), "2026-07-16T12:00:00Z"),
        (("2026-07-15", "20:00", "UTC"), "2026-07-15T20:01:00Z"),
        # No time: the end of the day. No zone: AoE, the latest day-end anywhere.
        (("2026-07-15", None, None), "2026-07-16T12:00:00Z"),
        (("2026-07-15", None, "UTC"), "2026-07-16T00:00:00Z"),
        (("2026-07-15", "17:00", None), "2026-07-16T05:01:00Z"),
        (("2026-07-15", "09:00", "UTC+9"), "2026-07-15T00:01:00Z"),
    ]
    got = _node({"instants": [list(c) for c, _ in cases]})["instants"]
    for (case, expected), ms in zip(cases, got):
        want = int(
            __import__("datetime").datetime.fromisoformat(expected.replace("Z", "+00:00")).timestamp()
            * 1000
        )
        assert ms == want, case
