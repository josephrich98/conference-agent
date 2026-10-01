"""The derived-month sorts roll from today instead of January 1.

Ascending starts at today's month and day and wraps around (on Oct 10: Oct 10-31,
Nov, ..., Sep, Oct 1-9), descending reverses that, and rows without a month stay
last either way. Within a month, rows order by the day of the month, ignoring the
year, so a prior edition shown for an unannounced series sorts by season. A start
of Jan 1 gives plain January-to-December order (the page's "Sort starting in
January" option).
Checked for both the API (``web/app.py``) and the static site
(``web/static/search.js``, via Node; skipped when ``node`` is unavailable).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

import web.app as appmod
from conference_agent.database import upsert_conferences
from conference_agent.models import Conference

_SEARCH_JS = Path(__file__).parent.parent / "web" / "static" / "search.js"

# Paper deadlines in Jan, Mar, Oct, Dec, plus one series with no paper deadline.
# The two October rows differ in year as well as day: last year's Oct 20 must
# still sort after this year's Oct 5.
_DEADLINES = {
    "JAN": "2027-01-15",
    "MAR": "2027-03-01",
    "OCTB": "2025-10-20",
    "OCTA": "2026-10-05",
    "DEC": "2026-12-01",
    "NONE": None,
}

# (start month, start day, order, expected). Starting Oct 1, ascending runs Oct,
# Dec, Jan, Mar; starting Oct 10, the Oct 5 deadline has passed this year and
# wraps to the end, after Mar.
_CASES = [
    (10, 1, "asc", ["OCTA", "OCTB", "DEC", "JAN", "MAR", "NONE"]),
    (10, 1, "desc", ["MAR", "JAN", "DEC", "OCTB", "OCTA", "NONE"]),
    (10, 10, "asc", ["OCTB", "DEC", "JAN", "MAR", "OCTA", "NONE"]),
    (10, 10, "desc", ["OCTA", "MAR", "JAN", "DEC", "OCTB", "NONE"]),
    (1, 1, "asc", ["JAN", "MAR", "OCTA", "OCTB", "DEC", "NONE"]),
    (1, 1, "desc", ["DEC", "OCTB", "OCTA", "MAR", "JAN", "NONE"]),
]


def test_api_month_sort_rolls_from_start_date(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("CONFERENCE_DATABASE_URL", url)
    monkeypatch.setattr(appmod, "_ensure_seeded", lambda: None)
    upsert_conferences(
        [
            Conference(
                acronym=k, name=f"{k} meeting", subcategory="radiology",
                upcoming_paper_deadline=date.fromisoformat(d) if d else None,
            )
            for k, d in _DEADLINES.items()
        ],
        url,
    )

    for month, day, order, expected in _CASES:
        rows = appmod._run_search(
            "", "paper_month", order, month_start=month, day_start=day
        )
        assert [r.acronym for r in rows] == expected, (month, day, order)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_month_sort_rolls_from_start_date():
    rows = [
        {
            "id": k, "acronym": k, "upcoming_paper_deadline": d,
            "paper_month": int(d[5:7]) if d else None,
        }
        for k, d in _DEADLINES.items()
    ]
    script = (
        f"const {{ sortRows }} = require({json.dumps(str(_SEARCH_JS))});"
        f"const rows = {json.dumps(rows)};"
        f"const cases = {json.dumps([[m, d, o] for m, d, o, _ in _CASES])};"
        "const out = cases.map(([m, d, o]) => sortRows(rows, 'paper_month', o, m, d).map((r) => r.id));"
        "process.stdout.write(JSON.stringify(out));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [expected for *_, expected in _CASES]
