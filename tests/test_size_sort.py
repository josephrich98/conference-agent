"""The size sort breaks ties within a bucket on attendance, not the acronym.

Ascending size runs massive -> small, so within a bucket the larger attendance
comes first; descending reverses both. Rows without a size stay last either way.
Checked for both the API (``web/app.py``) and the static site
(``web/static/search.js``, via Node; skipped when ``node`` is unavailable).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import web.app as appmod
from conference_agent.database import upsert_conferences
from conference_agent.models import Conference, size_for_attendance

_SEARCH_JS = Path(__file__).parent.parent / "web" / "static" / "search.js"

# Acronyms run opposite to attendance within each bucket, so an alphabetical
# tie-break would give the wrong order.
_ATTENDANCE = {
    "AMASS": 12000,
    "ZMASS": 45000,
    "ALARGE": 1500,
    "MLARGE": 6000,
    "ZLARGE": 9000,
    "SMALL": 40,
    "NONE": None,
}

_CASES = [
    ("asc", ["ZMASS", "AMASS", "ZLARGE", "MLARGE", "ALARGE", "SMALL", "NONE"]),
    ("desc", ["SMALL", "ALARGE", "MLARGE", "ZLARGE", "AMASS", "ZMASS", "NONE"]),
]


def test_api_size_sort_breaks_ties_on_attendance(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("CONFERENCE_DATABASE_URL", url)
    upsert_conferences(
        [
            Conference(acronym=k, name=f"{k} meeting", subcategory="radiology", attendance=n)
            for k, n in _ATTENDANCE.items()
        ],
        url,
    )

    for order, expected in _CASES:
        rows = appmod._run_search("", "size", order)
        assert [r.acronym for r in rows] == expected, order


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_size_sort_breaks_ties_on_attendance():
    rows = [
        {
            "id": k, "acronym": k, "attendance": n,
            "size": size_for_attendance(n).value if n is not None else None,
        }
        for k, n in _ATTENDANCE.items()
    ]
    script = (
        f"const {{ sortRows }} = require({json.dumps(str(_SEARCH_JS))});"
        f"const rows = {json.dumps(rows)};"
        f"const orders = {json.dumps([o for o, _ in _CASES])};"
        "const out = orders.map((o) => sortRows(rows, 'size', o).map((r) => r.id));"
        "process.stdout.write(JSON.stringify(out));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [expected for _, expected in _CASES]
