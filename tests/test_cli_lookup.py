"""Offline tests for ``conference-agent lookup`` (unique column values over a search)."""

from datetime import date

import pytest

from conference_agent.cli import main
from conference_agent.database import upsert_conferences
from conference_agent.models import Conference


@pytest.fixture
def url(tmp_path):
    db = f"sqlite:///{tmp_path / 'test.db'}"
    upsert_conferences(
        [
            Conference(acronym="ZZA", name="Zeta Imaging Meeting",
                       subcategory="radiology, pediatrics", attendance=20000,
                       upcoming_start_date=date(2099, 11, 1)),
            Conference(acronym="ZZB", name="Zeta Optics Workshop", subcategory="optics",
                       attendance=100, upcoming_start_date=date(2099, 3, 1)),
            Conference(acronym="ZZC", name="Zeta Radiology Forum", subcategory="radiology",
                       attendance=500, upcoming_start_date=date(2099, 5, 1)),
            # Retired: last edition long past and nothing upcoming.
            Conference(acronym="ZZD", name="Zeta Old Symposium", subcategory="radiology",
                       prior_start_date=date(2001, 1, 1)),
        ],
        db_url=db,
    )
    return db


def _lookup(capsys, url, *args):
    code = main(["--db", url, "lookup", *args])
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err


def test_single_column_unique_values_split_tags(capsys, url):
    code, lines, _ = _lookup(capsys, url, "--columns", "subcategory")
    assert code == 0
    assert lines == ["optics", "pediatrics", "radiology"]


def test_sort_orders(capsys, url):
    _, lines, _ = _lookup(capsys, url, "--columns", "acronym", "--sort", "reversealphabetical")
    assert lines == ["ZZC", "ZZB", "ZZA"]  # ZZD is retired
    _, lines, _ = _lookup(capsys, url, "--columns", "size", "--sort", "increasing")
    assert lines == ["small", "medium", "massive"]
    _, lines, _ = _lookup(capsys, url, "--columns", "attendance", "--sort", "decreasing")
    assert lines == ["20000", "500", "100"]


def test_boolean_query_and_multiple_columns(capsys, url):
    code, lines, _ = _lookup(
        capsys, url, "--columns", "acronym", "conference_month",
        "--query", "subcategory:radiology AND attendance>=1000",
    )
    assert code == 0
    assert lines == ["acronym:", "  ZZA", "", "conference_month:", "  11"]


def test_keyword_fallback_when_boolean_matches_nothing(capsys, url):
    code, lines, err = _lookup(capsys, url, "--columns", "acronym", "--query", "radiolgy")
    assert code == 0
    assert lines == ["ZZA", "ZZC"]  # the typo still finds both radiology rows
    assert "keyword" in err


def test_include_retired(capsys, url):
    _, lines, _ = _lookup(capsys, url, "--columns", "acronym", "--include-retired")
    assert lines == ["ZZA", "ZZB", "ZZC", "ZZD"]


def test_unknown_column_fails(capsys, url):
    code, _, err = _lookup(capsys, url, "--columns", "bogus")
    assert code == 1
    assert "bogus" in err
