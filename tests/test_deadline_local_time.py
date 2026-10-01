"""Tests for the viewer-local deadline time shown under the "Deadline time" column.

``localDeadlineTime`` in ``web/static/calendar.js`` takes a stored 24-hour time and
zone code ("23:59", "ET") and renders it in the viewer's zone. It is browser-only,
so it is exercised through Node; skipped when ``node`` is unavailable (e.g. on the
Python-only CI).
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
    # (time, zone code, date, viewer zone, military, expected)
    ("23:59", "ET", "2026-07-15", LA, False, "8:59 PM PDT"),
    ("23:59", "ET", "2026-01-15", LA, False, "8:59 PM PST"),
    ("23:59", "CET", "2026-07-15", LA, False, "2:59 PM PDT"),
    ("23:59", "AoE", "2026-07-15", LA, False, "4:59 AM PDT the next day"),
    ("01:00", "CET", "2026-01-10", "America/Chicago", False, "6:00 PM CST the previous day"),
    ("12:00", "CT", "2026-07-15", LA, False, "10:00 AM PDT"),
    ("12:00", "CET", "2026-01-15", "America/New_York", False, "6:00 AM EST"),
    ("21:00", "UTC-5", "2026-07-15", LA, False, "7:00 PM PDT"),
    ("22:00", "UTC", "2026-07-15", LA, False, "3:00 PM PDT"),
    ("20:00", "Asia/Shanghai", "2026-07-15", LA, False, "5:00 AM PDT"),
    # Military display of the viewer-local time.
    ("23:59", "ET", "2026-07-15", LA, True, "20:59 PDT"),
    ("23:59", "AoE", "2026-07-15", LA, True, "04:59 PDT the next day"),
    # Already in the viewer's offset, or no time / no usable zone: nothing to add.
    ("23:59", "PT", "2026-07-15", LA, False, None),
    ("17:00", None, "2026-07-15", LA, False, None),
    (None, "ET", "2026-07-15", LA, False, None),
    ("17:00", "Nowhere", "2026-07-15", LA, False, None),
]


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_local_deadline_time():
    payload = json.dumps([list(c[:5]) for c in _CASES])
    out = subprocess.run(
        [_NODE, str(_RUNNER)], input=payload, capture_output=True, text=True, check=True
    )
    got = json.loads(out.stdout)
    for (time, tz, date, zone, military, expected), actual in zip(_CASES, got):
        assert actual == expected, f"{time} {tz} on {date} in {zone} (military={military})"
