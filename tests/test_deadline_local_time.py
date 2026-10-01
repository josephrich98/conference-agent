"""Tests for the viewer-local deadline time shown under the "Deadline time" column.

``localDeadlineTime`` in ``web/static/calendar.js`` parses the free-text
``deadline_time`` ("11:59 PM ET", "23:59 AoE") and renders it in the viewer's
zone. It is browser-only, so it is exercised through Node; skipped when ``node``
is unavailable (e.g. on the Python-only CI).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_RUNNER = Path(__file__).parent / "js" / "run_local_time.js"

LA = "America/Los_Angeles"

_CASES = [
    # (text, date, viewer zone, expected)
    ("11:59 PM ET", "2026-07-15", LA, "8:59 PM PDT"),
    ("11:59 PM ET", "2026-01-15", LA, "8:59 PM PST"),
    ("23:59 CEST", "2026-07-15", LA, "2:59 PM PDT"),
    ("23:59 AoE", "2026-07-15", LA, "4:59 AM PDT the next day"),
    ("AoE (12:00 noon UTC the following day)", "2026-07-15", LA, "4:59 AM PDT the next day"),
    ("11:59 PM UTC-12 (AoE)", "2026-07-15", LA, "4:59 AM PDT the next day"),
    ("1:00 AM CET", "2026-01-10", "America/Chicago", "6:00 PM CST the previous day"),
    ("12 p.m. CT", "2026-07-15", LA, "10:00 AM PDT"),
    ("12:00 PM (Noon) EST", "2026-07-15", LA, "9:00 AM PDT"),
    ("Noon CET", "2026-01-15", "America/New_York", "6:00 AM EST"),
    ("midnight CEST", "2026-07-15", LA, "2:59 PM PDT"),
    ("21:00 Eastern time (UTC-5)", "2026-07-15", LA, "6:00 PM PDT"),
    ("10:00 PM UTC", "2026-07-15", LA, "3:00 PM PDT"),
    # Already in the viewer's offset, or unparseable: nothing to add.
    ("11:59 PM PT", "2026-07-15", LA, None),
    ("11:59 PM PST", "2026-07-15", LA, None),
    ("5:00 PM", "2026-07-15", LA, None),
    ("EOD", "2026-07-15", LA, None),
]


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_local_deadline_time():
    payload = json.dumps([[t, d, z] for t, d, z, _ in _CASES])
    out = subprocess.run(
        [_NODE, str(_RUNNER)], input=payload, capture_output=True, text=True, check=True
    )
    got = json.loads(out.stdout)
    for (text, date, zone, expected), actual in zip(_CASES, got):
        assert actual == expected, f"{text!r} on {date} in {zone}"
