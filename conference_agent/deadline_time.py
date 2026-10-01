"""Structured deadline times: a time of day and a time zone per deadline kind.

Each series stores, for its abstract / late abstract / paper deadlines, a
24-hour ``HH:MM`` time and a time-zone code (``abstract_time`` /
``abstract_timezone``, ...). Those columns are the source of truth; the
user-facing ``Deadline time`` string ("11:59 PM ET") is *derived* from them by
:func:`format_deadline_time`, so it can never drift and the browser never has to
parse free text.

Input is forgiving (any of "11:59 PM", "23:59", "5 p.m.", "noon"; any of "ET",
"EST", "Eastern Time", "UTC-5", "America/New_York") but storage is canonical:
times are always 24-hour ``HH:MM`` and zones are always one of the codes in
:data:`TIMEZONES` or an IANA name. Display format (12- or 24-hour) is a viewing
choice, so the stored form carries no preference.

The zone table is mirrored in ``web/static/calendar.js`` (``DEADLINE_ZONES``);
``tests/test_deadline_time.py`` pins the two together.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

# The three deadline kinds, in display order, with the model-field prefix each
# one's time / timezone columns use (``abstract_time``, ``late_abstract_timezone``).
KINDS = ("abstract", "late_abstract", "paper")

# Canonical zone codes -> how to resolve each. ``iana`` zones observe DST (the
# code names the region's wall clock, so "ET" is correct in summer too);
# ``offset`` zones are fixed minutes from UTC. Organizers routinely write "EST" or
# "CET" through the summer while meaning local time, so the standard/daylight
# abbreviations all collapse onto the region's code.
TIMEZONES: Dict[str, dict] = {
    "AoE": {"offset": -12 * 60},  # Anywhere on Earth (UTC-12)
    "UTC": {"offset": 0},
    "ET": {"iana": "America/New_York"},
    "CT": {"iana": "America/Chicago"},
    "MT": {"iana": "America/Denver"},
    "PT": {"iana": "America/Los_Angeles"},
    "AKT": {"iana": "America/Anchorage"},
    "HST": {"iana": "Pacific/Honolulu"},
    "CET": {"iana": "Europe/Berlin"},
    "WET": {"iana": "Europe/Lisbon"},
    "EET": {"iana": "Europe/Athens"},
    "UK": {"iana": "Europe/London"},
    "IST": {"iana": "Asia/Kolkata"},
    "SGT": {"iana": "Asia/Singapore"},
    "JST": {"iana": "Asia/Tokyo"},
    "KST": {"iana": "Asia/Seoul"},
    "AEST": {"iana": "Australia/Sydney"},
    "BRT": {"iana": "America/Sao_Paulo"},
}

# Free-text zone spellings -> canonical code, tried in order (first match wins).
# ``(pattern, flags, code)``; the signed-offset form is handled before this list.
_ZONE_ALIASES: Tuple[Tuple[str, int, str], ...] = (
    (r"\bAoE\b|anywhere on earth", re.I, "AoE"),
    (r"\b(?:UTC|GMT|Z)\b", 0, "UTC"),
    (r"\bE[SD]?T\b|\beastern\b", re.I, "ET"),
    (r"\bC[SD]?T\b|\bcentral(?! europe)\b", re.I, "CT"),
    (r"\bM[SD]?T\b|\bmountain\b", re.I, "MT"),
    (r"\bP[SD]?T\b|\bpacific\b", re.I, "PT"),
    (r"\bAK[SD]?T\b|\balaska\b", re.I, "AKT"),
    (r"\bHST\b|\bhawaii\b", re.I, "HST"),
    (r"\bCES?T\b|\bcentral europe", re.I, "CET"),
    (r"\bWES?T\b", 0, "WET"),
    (r"\bEES?T\b", 0, "EET"),
    (r"\bBST\b|\bUK time\b|\bLondon\b", re.I, "UK"),
    (r"\bIST\b", 0, "IST"),
    (r"\bSGT\b", 0, "SGT"),
    (r"\bJST\b", 0, "JST"),
    (r"\bKST\b", 0, "KST"),
    (r"\bAE[SD]T\b", 0, "AEST"),
    (r"\bBRT\b", 0, "BRT"),
)

_SIGNED_OFFSET = re.compile(r"\b(?:UTC|GMT)\s*([+\-−])\s*(\d{1,2})(?::?(\d{2}))?\b", re.I)
_IANA = re.compile(r"^[A-Za-z]+(?:/[A-Za-z0-9_+\-]+)+$")
_TIME = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)?(?![\d.])", re.I
)
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _offset_code(minutes: int) -> str:
    """Canonical code for a fixed UTC offset: AoE / UTC, else ``UTC+9`` / ``UTC-5:30``."""
    for code, spec in TIMEZONES.items():
        if spec.get("offset") == minutes:
            return code
    sign = "+" if minutes > 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


def normalize_timezone(value: Optional[str]) -> Optional[str]:
    """Canonical zone for free text, or ``None`` when no zone can be read.

    Returns a code from :data:`TIMEZONES`, a fixed offset (``UTC+9``), or a
    validated IANA name ("Asia/Shanghai"). Parenthetical asides are ignored unless
    the zone appears only there ("11:59 PM CDT (UTC-5)" -> ``CT``; "AoE (12:00
    noon UTC the following day)" -> ``AoE``).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in TIMEZONES:
        return text
    if _IANA.match(text):
        return text if _valid_iana(text) else None
    main = re.sub(r"\([^)]*\)", " ", text)
    for source in (main, text):
        m = _SIGNED_OFFSET.search(source)
        if m:
            sign = 1 if m.group(1) == "+" else -1
            return _offset_code(sign * (int(m.group(2)) * 60 + int(m.group(3) or 0)))
        for pattern, flags, code in _ZONE_ALIASES:
            if re.search(pattern, source, flags):
                return code
    return None


def _valid_iana(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:  # unknown zone, or no tz database on this machine
        return False


def normalize_time(value: Optional[str]) -> Optional[str]:
    """Canonical 24-hour ``HH:MM`` for free text, or ``None`` when no time is read.

    Accepts 24-hour ("23:59", "9:00"), 12-hour ("11:59 PM", "5 p.m.", "11:59pm"),
    "noon", and "midnight" / "end of day" (the end of the stated day, ``23:59``).
    Seconds are dropped. Parenthetical asides are ignored.
    """
    if value is None:
        return None
    text = re.sub(r"\([^)]*\)", " ", str(value)).strip()
    if not text:
        return None
    m = _TIME.search(text)
    if m and (m.group(2) is not None or m.group(3)):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if ampm and not 1 <= int(m.group(1)) <= 12:
            return None
    elif re.search(r"\bnoon\b", text, re.I):
        hour, minute = 12, 0
    elif re.search(r"\bmidnight\b|\bend of (?:the )?day\b|\bEOD\b|\bAoE\b", text, re.I):
        hour, minute = 23, 59
    else:
        return None
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def format_time(time: str, military: bool = False) -> str:
    """Display a stored ``HH:MM`` as ``"8:00 PM"`` (default) or ``"20:00"``."""
    m = _CLOCK.match(time)
    if not m:
        return time
    hour, minute = int(m.group(1)), m.group(2)
    if military:
        return f"{hour:02d}:{minute}"
    return f"{(hour % 12) or 12}:{minute} {'AM' if hour < 12 else 'PM'}"


def format_spec(time: Optional[str], timezone: Optional[str], military: bool = False) -> str:
    """One deadline's time as shown: ``"8:00 PM AoE"``, ``"20:00 AoE"``, or ``""``."""
    parts = [format_time(time, military) if time else "", timezone or ""]
    return " ".join(p for p in parts if p)


Spec = Tuple[Optional[str], Optional[str]]  # (time "HH:MM", timezone code)


def deadline_entries(
    specs: Dict[str, Spec], dated: Iterable[str]
) -> List[Tuple[Optional[str], Spec]]:
    """The display entries for a series, collapsing identical times into one.

    ``specs`` maps each kind in :data:`KINDS` to its ``(time, timezone)``;
    ``dated`` lists the kinds the series has any deadline date for. A kind is
    *considered* when it has a time recorded or a date. When every considered kind
    shares one non-empty spec, a single unlabeled entry ``(None, spec)`` is
    returned ("8:00 PM ET", not "abstract: 8:00 PM ET; paper: 8:00 PM ET").
    Otherwise one ``(kind, spec)`` entry per kind that has a spec, in
    :data:`KINDS` order -- a kind with a date but no recorded time is simply
    omitted, so the labeled lines never claim a time that was not published.
    """
    dated = set(dated)
    present = {k: s for k, s in specs.items() if s[0] or s[1]}
    considered = [k for k in KINDS if k in present or k in dated]
    values = {specs.get(k, (None, None)) for k in considered}
    if len(values) == 1 and next(iter(values)) != (None, None):
        return [(None, next(iter(values)))]
    return [(k, present[k]) for k in KINDS if k in present]


def format_deadline_time(
    specs: Dict[str, Spec], dated: Iterable[str], military: bool = False
) -> Optional[str]:
    """The derived ``deadline_time`` string, or ``None`` when no time is recorded.

    One line per entry from :func:`deadline_entries`; labeled lines read
    ``"abstract: 11:59 PM ET"`` / ``"late abstract: ..."`` / ``"paper: ..."``.
    """
    lines = []
    for kind, (time, tz) in deadline_entries(specs, dated):
        text = format_spec(time, tz, military)
        if text:
            lines.append(f"{kind.replace('_', ' ')}: {text}" if kind else text)
    return "\n".join(lines) or None


# ---------------------------------------------------------------------------
# Legacy free text -> structured (one-time migration + the ``deadline_time`` input)
# ---------------------------------------------------------------------------

_LABELS = {
    "abstract": "abstract",
    "late abstract": "late_abstract",
    "late-abstract": "late_abstract",
    "late_abstract": "late_abstract",
    "paper": "paper",
}
_LABELED = re.compile(r"^\s*([A-Za-z][A-Za-z _-]*?)\s*:\s*(.+)$")


def parse_entry(text: str) -> Spec:
    """``(time, timezone)`` from one free-text entry ("11:59 PM ET"); either may be ``None``.

    A bare end-of-day word ("EOD", "midnight") with no zone says nothing useful,
    so it yields no time; "AoE" alone means 23:59 AoE by convention.
    """
    tz = normalize_timezone(text)
    time = normalize_time(text)
    if time and tz is None and not re.search(r"\d", re.sub(r"\([^)]*\)", " ", text)) \
            and not re.search(r"\bnoon\b", text, re.I):
        time = None
    return time, tz


def parse_legacy_deadline_time(text: Optional[str], dated: Iterable[str] = ()) -> Dict[str, Optional[str]]:
    """Structured fields from the old free-text ``deadline_time``.

    Returns a dict keyed ``abstract_time`` / ``abstract_timezone`` / ... for every
    kind that got a value. Entries are split on newlines / semicolons; a labeled
    entry ("late abstract: 5 PM ET") sets its own kind. An unlabeled entry applies
    to every kind in ``dated`` (the kinds the series has a deadline date for), or to
    the abstract when no kind is dated -- so a series with no paper deadline is not
    given a paper time it never published.
    """
    out: Dict[str, Optional[str]] = {}
    if not text or not str(text).strip():
        return out
    dated = [k for k in KINDS if k in set(dated)] or ["abstract"]
    for entry in re.split(r"[\n;]", str(text)):
        entry = entry.strip()
        if not entry:
            continue
        m = _LABELED.match(entry)
        kind = _LABELS.get(m.group(1).strip().lower()) if m else None
        body = m.group(2) if kind else entry
        time, tz = parse_entry(body)
        if not (time or tz):
            continue
        for k in ([kind] if kind else dated):
            out.setdefault(f"{k}_time", time)
            out.setdefault(f"{k}_timezone", tz)
    return {k: v for k, v in out.items() if v}
