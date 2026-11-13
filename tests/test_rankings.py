"""Offline tests for the CCF / CORE ranking fields and the fill script's matching."""

import importlib.util
import sys
from pathlib import Path

import pytest

from conference_agent.cli import main
from conference_agent.database import (
    apply_refreshed_conferences,
    merge_records,
    query_conferences,
    set_pins,
    upsert_conferences,
)
from conference_agent.models import Conference, normalize_ccf_rank, normalize_core_rank


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _stored(url, acronym="ICML"):
    return next(c for c in query_conferences(db_url=url) if c.acronym == acronym)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("A", "A"), ("b", "B"), ("CCF-A", "A"), ("CCF C", "C"), ("A类", "A"), ("", None), (None, None)],
)
def test_normalize_ccf_rank(value, expected):
    assert normalize_ccf_rank(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("A*", "A*"), ("a*", "A*"), ("CORE A", "A"), ("C", "C"),
        ("National: USA", None), ("Australasian B", None), ("Unranked", None),
        ("National/Regional", None), ("Journal Published", None), ("Multiconference", None),
    ],
)  # fmt: skip
def test_normalize_core_rank(value, expected):
    assert normalize_core_rank(value) == expected


@pytest.mark.parametrize("value", ["A*", "D", "top"])
def test_invalid_ccf_rank_raises(value):
    with pytest.raises(ValueError):
        normalize_ccf_rank(value)


def test_invalid_core_rank_raises():
    with pytest.raises(ValueError):
        normalize_core_rank("B+")
    with pytest.raises(ValueError):
        Conference(acronym="X", name="X", subcategory="ml", core_rank="Q")


def _icml(**extra):
    return Conference(
        acronym="ICML", name="International Conference on Machine Learning",
        subcategory="machine learning", **extra,
    )


def test_discovery_never_clears_stored_ranks(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_icml(ccf_rank="A", core_rank="A*")], db_url=url, manual=True)
    # A survey reports no ranks (discovery does not research them).
    upsert_conferences([_icml(attendance=9000)], db_url=url)
    got = _stored(url)
    assert (got.ccf_rank, got.core_rank, got.attendance) == ("A", "A*", 9000)
    # Nor does a targeted re-check.
    apply_refreshed_conferences([_icml(attendance=9500)], db_url=url)
    got = _stored(url)
    assert (got.ccf_rank, got.core_rank, got.attendance) == ("A", "A*", 9500)


def test_merge_records_fills_without_clearing(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_icml()], db_url=url)
    merge_records([{"name": _icml().name, "ccf": "a", "core_rank": "A*"}], db_url=url)
    assert (_stored(url).ccf_rank, _stored(url).core_rank) == ("A", "A*")
    # A blank, an off-scale CORE label, or an invalid value never clears one.
    merge_records([{"name": _icml().name, "ccf_rank": "", "core_rank": "Unranked"}], db_url=url)
    merge_records([{"name": _icml().name, "ccf_rank": "Z"}], db_url=url)
    assert (_stored(url).ccf_rank, _stored(url).core_rank) == ("A", "A*")


def test_ranks_are_pinnable(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_icml(core_rank="A*")], db_url=url, manual=True)
    assert set_pins(_icml().id, pin=["core_rank"], db_url=url) == ["core_rank"]


def test_add_flags_csv_and_validation(tmp_path):
    url = _db_url(tmp_path)
    assert main([
        "--db", url, "add", "--conference-acronym", "ICML", "--conference-name",
        _icml().name, "--subcategory", "machine learning", "--ccf", "a", "--core", "A*",
    ]) == 0  # fmt: skip
    assert (_stored(url).ccf_rank, _stored(url).core_rank) == ("A", "A*")

    csv_path = tmp_path / "in.csv"
    csv_path.write_text("conference_name,core_rank\nInternational Conference on Machine Learning,A\n")
    assert main(["--db", url, "add", "--update", "--csv", str(csv_path)]) == 0
    assert _stored(url).core_rank == "A"

    bad = tmp_path / "bad.json"
    bad.write_text('[{"conference_name": "International Conference on Machine Learning", "ccf": "A*"}]')
    assert main(["--db", url, "add", "--update", "--json", str(bad)]) == 1
    assert _stored(url).ccf_rank == "A"


def _fill_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fill_rankings.py"
    spec = importlib.util.spec_from_file_location("fill_rankings", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def test_fill_matching_requires_acronym_and_name():
    fill = _fill_module()
    Entry = fill.Entry
    entries = [
        Entry("ICML", "International Conference on Machine Learning", "A*"),
        Entry("ATS", "IEEE Asian Test Symposium", "C"),
        Entry("UAI", "Conference on Uncertainty in ArtificialIntelligence", "B"),
        Entry("PSB", "Pacific Symposium on Biocomputing", ""),
        Entry("ISC", "Information Security Conference", "B"),
        Entry("ISC", "International Supercomputing Conference", "C"),
    ]
    assert fill.match("ICML", "International Conference on Machine Learning", entries).rank == "A*"
    # Same acronym, unrelated name: left blank.
    m = fill.match("ATS", "American Thoracic Society International Conference", entries)
    assert m.rank is None and m.note.startswith("acronym only")
    # Words run together in the source still match.
    assert fill.match("UAI", "Conference on Uncertainty in Artificial Intelligence", entries).rank == "B"
    # Listed with a label off the A*-C scale.
    assert fill.match("PSB", "Pacific Symposium on Biocomputing", entries).rank is None
    # Not listed at all.
    assert fill.match("RSNA", "Radiological Society of North America", entries).note == "not listed"
