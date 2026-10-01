"""Tests for the browser-only keyword fallback in ``web/static/search.js``.

When the exact boolean search matches nothing (or the text does not parse), the
page ranks rows with ``keywordSearch``, which tolerates typos, inflections, and
filler words. ``web.search.keyword_search`` is its Python port (used by
``conference-agent lookup``); the parity test below pins the two together. The JS
tests run through Node and are skipped automatically when ``node`` is unavailable (e.g. on the Python-only CI).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from web.search import keyword_search, keyword_terms

_NODE = shutil.which("node")
_RUNNER = Path(__file__).parent / "js" / "run_keyword.js"

_ROWS = [
    dict(
        id="RSNA", acronym="RSNA", name="Radiological Society of North America Annual Meeting",
        subcategory="radiology", category="medicine", size="large", remote_option="in-person",
        upcoming_location="McCormick Place, Chicago, Illinois, USA",
        upcoming_start_date="2026-11-29", conference_month=11,
    ),
    dict(
        id="SPR", acronym="SPR", name="Society for Pediatric Radiology Annual Meeting",
        subcategory="radiology, pediatrics", category="medicine", size="medium",
        remote_option="in-person", upcoming_location="Dallas, TX", upcoming_start_date="2026-05-12",
        conference_month=5,
    ),
    dict(
        id="ECR", acronym="ECR", name="European Congress of Radiology",
        subcategory="radiology", category="medicine", size="large", remote_option="hybrid",
        upcoming_location="Vienna, Austria", upcoming_start_date="2027-03-03", conference_month=3,
    ),
    dict(
        id="NEURIPS", acronym="NeurIPS", name="Conference on Neural Information Processing Systems",
        subcategory="machine learning", category="artificial intelligence", size="large",
        remote_option="hybrid", upcoming_location="Sydney, Australia", upcoming_start_date="2026-12-06",
        conference_month=12,
    ),
    dict(
        id="AAN", acronym="AAN", name="American Academy of Neurology Annual Meeting",
        subcategory="neurology", category="medicine", size="large", remote_option="hybrid",
        upcoming_location="Chicago, IL", upcoming_start_date="2027-04-17", conference_month=4,
    ),
    dict(
        id="SITC", acronym="SITC", name="Society for Immunotherapy of Cancer Annual Meeting",
        subcategory="oncology", category="medicine", size="large", remote_option="in-person",
        upcoming_location="Phoenix, Arizona, USA", conference_month=11,
    ),
    dict(
        id="JSM", acronym="JSM", name="Joint Statistical Meetings",
        subcategory="statistics", category="stats", size="large", remote_option="in-person",
        upcoming_location="Boston, MA, USA", conference_month=8,
    ),
]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if _NODE is None:
        pytest.skip("node is required for the JS keyword-search tests")
    rows_path = tmp_path_factory.mktemp("kw") / "rows.json"
    rows_path.write_text(json.dumps(_ROWS), encoding="utf-8")

    def _run(*queries: str) -> dict:
        proc = subprocess.run(
            [_NODE, str(_RUNNER), str(rows_path)],
            input=json.dumps(list(queries)),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"node runner failed:\n{proc.stderr}"
        return json.loads(proc.stdout)

    return _run


def test_terms_drop_filler_words_operators_and_field_names(run):
    out = run(
        "pediatric radiology conferences in europe",
        "radiology AND NOT virtual",
        'subcategory:radiolgy "machine learning"',
        "conference_dates:>=2026-06-01",
        "the conferences",
    )
    assert out["pediatric radiology conferences in europe"]["terms"] == [
        "pediatric", "radiology", "europe"
    ]
    assert out["radiology AND NOT virtual"]["terms"] == ["radiology", "virtual"]
    assert out['subcategory:radiolgy "machine learning"']["terms"] == [
        "machine learning", "radiolgy"
    ]
    assert out["conference_dates:>=2026-06-01"]["terms"] == ["2026"]
    assert out["the conferences"] == {"terms": [], "ids": []}


def test_typos_and_inflections_match(run):
    out = run("radiolgy", "nuerology", "radiological", "pediatrics")
    assert set(out["radiolgy"]["ids"]) == {"RSNA", "SPR", "ECR"}
    assert out["nuerology"]["ids"] == ["AAN"]
    assert set(out["radiological"]["ids"]) == {"RSNA", "SPR", "ECR"}
    assert out["pediatrics"]["ids"] == ["SPR"]


def test_short_words_are_not_fuzzy_matched(run):
    # "stats" must not match "States" by edit distance; it matches the category.
    assert run("stats")["stats"]["ids"] == ["JSM"]


def test_rows_matching_more_terms_rank_first(run):
    ids = run("pediatric radiology in europe")["pediatric radiology in europe"]["ids"]
    assert ids[:2] in (["SPR", "ECR"], ["ECR", "SPR"])
    assert set(ids[2:]) <= {"RSNA"}


def test_synonyms_months_and_years(run):
    out = run("ai december", "cancer: immunotherapy", "chicago 2027")
    assert out["ai december"]["ids"][0] == "NEURIPS"
    assert out["cancer: immunotherapy"]["ids"] == ["SITC"]
    assert out["chicago 2027"]["ids"][0] == "AAN"


def test_no_match_returns_empty(run):
    assert run("zzzzqqq")["zzzzqqq"]["ids"] == []


_PARITY_QUERIES = (
    "pediatric radiology conferences in europe",
    "radiology AND NOT virtual",
    'subcategory:radiolgy "machine learning"',
    "conference_dates:>=2026-06-01",
    "the conferences",
    "radiolgy", "nuerology", "radiological", "pediatrics", "stats",
    "ai december", "cancer: immunotherapy", "chicago 2027", "online neuro",
    "big usa", "zzzzqqq", "Vienna", "statistical boston",
)


def test_python_port_matches_js(run):
    out = run(*_PARITY_QUERIES)
    for q in _PARITY_QUERIES:
        assert keyword_terms(q) == out[q]["terms"], q
        assert [r["id"] for r in keyword_search(q, _ROWS)] == out[q]["ids"], q
