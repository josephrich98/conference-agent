"""Parity test: the browser query language matches the server query language.

The static site reimplements ``web/search.py`` in JavaScript (``web/static/search.js``)
so that filtering runs in the browser with no backend. The one real risk of that
migration is the two implementations drifting apart. This test compiles a corpus
of representative queries through *both* — the Python ``build_filter`` over an
in-memory SQLite catalog, and the JS ``buildPredicate`` over the same rows
exported as JSON — and asserts they select exactly the same conference ids.

Skipped automatically when ``node`` is unavailable (e.g. on the Python-only CI),
so it never breaks a hermetic run; execute it locally to guard against drift.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.database import Base, ConferenceRow, get_engine
from web.app import _row_to_dict
from web.search import build_filter

_NODE = shutil.which("node")
_RUNNER = Path(__file__).parent / "js" / "run_search.js"

# Representative rows exercising every queryable shape: multi-valued category /
# subcategory / format, each remote option and size, upcoming-only, prior-only,
# and no-date editions, null cost, free-text registration, and a spread of months
# and years for the date and month comparisons.
_ROWS = [
    dict(
        id="RSNA", acronym="RSNA", name="Radiological Society of North America",
        subcategory="radiology", category="medicine", size="large", attendance=45000,
        remote_option="in-person", cost="$$$ members; $$$$ non-members",
        upcoming_abstract_deadline=date(2026, 4, 1), upcoming_start_date=date(2026, 11, 29),
        upcoming_end_date=date(2026, 12, 4), upcoming_registration="Early bird: Jul 1 – Sep 1; Regular: Sep 2 –",
        url="https://rsna.org", format="abstract, poster",
    ),
    dict(
        id="MICCAI", acronym="MICCAI", name="Medical Image Computing and Computer Assisted Intervention",
        subcategory="radiology, machine learning", category="medicine, artificial intelligence",
        size="large", attendance=2500, remote_option="hybrid", cost="$$",
        upcoming_paper_deadline=date(2026, 3, 10), upcoming_start_date=date(2026, 9, 20),
        upcoming_end_date=date(2026, 9, 24), format="paper, oral",
        url="https://miccai.org",
    ),
    dict(
        id="NEURIPS", acronym="NeurIPS", name="Neural Information Processing Systems",
        subcategory="machine learning", category="artificial intelligence", size="large",
        attendance=15000, remote_option="virtual", cost=None,
        upcoming_paper_deadline=date(2026, 5, 15), upcoming_start_date=date(2026, 12, 7),
        upcoming_end_date=date(2026, 12, 13), format="paper",
    ),
    dict(
        id="SPR", acronym="SPR", name="Society for Pediatric Radiology",
        subcategory="radiology, pediatrics", category="medicine", size="medium",
        attendance=800, remote_option="in-person", cost="$",
        upcoming_abstract_deadline=date(2026, 1, 15), upcoming_start_date=date(2026, 5, 12),
        upcoming_end_date=date(2026, 5, 16), format="abstract",
    ),
    # Upcoming dates not yet announced — date queries must fall back to prior.
    dict(
        id="ESGAR", acronym="ESGAR", name="European Society of Gastrointestinal and Abdominal Radiology",
        subcategory="radiology", category="medicine", size="medium", attendance=600,
        remote_option="hybrid", cost="€€",
        prior_abstract_deadline=date(2025, 2, 1), prior_start_date=date(2025, 6, 10),
        prior_end_date=date(2025, 6, 13), format="abstract, poster",
    ),
    # No dates at all — fails every presence/date test.
    dict(
        id="TINYCONF", acronym="TINY", name="Tiny Workshop on Things",
        subcategory="statistics", category="stats", size="small", attendance=40,
        remote_option="unknown", cost=None, format=None,
    ),
    dict(
        id="JSM", acronym="JSM", name="Joint Statistical Meetings",
        subcategory="statistics", category="stats", size="large", attendance=6000,
        remote_option="in-person", cost="$$$",
        upcoming_abstract_deadline=date(2026, 2, 2), upcoming_start_date=date(2026, 8, 1),
        upcoming_end_date=date(2026, 8, 6), format="abstract, oral",
    ),
    dict(
        id="ASCO", acronym="ASCO", name="American Society of Clinical Oncology",
        subcategory="oncology", category="medicine", size="large", attendance=40000,
        remote_option="hybrid", cost="$$$$",
        upcoming_abstract_deadline=date(2027, 2, 10), upcoming_start_date=date(2027, 6, 4),
        upcoming_end_date=date(2027, 6, 8), format="abstract",
        upcoming_registration="Member: opens Jan; Non-member: opens Feb",
    ),
]

_QUERIES = [
    "",
    "radiology",
    "Radiology",
    "imaging",
    '"Pediatric Radiology"',
    "category:medicine",
    "category:artificial intelligence",
    "subcategory:radiology",
    "subcategory:pediatrics",
    "size:large",
    "size:medium",
    "remote:virtual",
    "remote:hybrid",
    "format:poster",
    "format:oral",
    "cost:*",
    "NOT cost:*",
    "registration:*",
    "registration:bird",
    "conference_dates:*",
    "NOT conference_dates:*",
    "abstract_due:*",
    "(virtual OR hybrid) AND size:large",
    "subcategory:radiology NOT remote:virtual",
    "category:medicine AND category:artificial intelligence",
    "size:large radiology",
    "conference_dates:>=2026-06-01",
    "conference_dates:<2026",
    "conference_dates:2026",
    "conference_dates:2026-09",
    "abstract_due:<2026-03-01",
    "abstract_due:>=2027",
    "conference_dates:>=2025-06-10 AND conference_dates:<=2025-12-31",
    "conference_month:11",
    "conference_month:>=8",
    "conference_month:nov",
    "abstract_month:<=February",
    "abstract_month>=4",
    "paper_month:may",
    "conference:RSNA",
    "conference:neurips",
    "name:oncology",  # alias for conference
    "deadline:<2026",  # alias for abstract_due
    "date:2026",       # alias for conference_dates
    "(category:medicine OR category:stats) AND NOT remote:virtual",
]


def _make_db(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'parity.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for row in _ROWS:
            session.add(ConferenceRow(**row))
        session.commit()
    return url


@pytest.mark.skipif(_NODE is None, reason="node is required for the JS parity check")
def test_browser_search_matches_server(tmp_path):
    url = _make_db(tmp_path)
    engine = get_engine(url)

    python_results: dict[str, list[str]] = {}
    with Session(engine) as session:
        for query in _QUERIES:
            filt = build_filter(query)
            stmt = select(ConferenceRow.id)
            if filt is not None:
                stmt = stmt.where(filt)
            python_results[query] = sorted(session.scalars(stmt))

        rows = list(session.scalars(select(ConferenceRow)))
        dicts = [_row_to_dict(r) for r in rows]

    rows_path = tmp_path / "rows.json"
    rows_path.write_text(json.dumps(dicts), encoding="utf-8")

    proc = subprocess.run(
        [_NODE, str(_RUNNER), str(rows_path)],
        input=json.dumps(_QUERIES),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stderr}"
    js_results = json.loads(proc.stdout)

    mismatches = []
    for query in _QUERIES:
        js = js_results[query]
        py = python_results[query]
        if js != py:
            mismatches.append(f"  {query!r}: js={js} python={py}")
    assert not mismatches, "browser/server query mismatch:\n" + "\n".join(mismatches)
