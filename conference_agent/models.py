"""Typed schema for a conference.

One ``Conference`` record describes a recurring conference *series* (e.g. RSNA),
holding both its most recent **prior** edition and its **upcoming** edition.
Series are indexed by their full name -- two series may share an acronym -- and
the record id is a URL-safe slug of it (:func:`name_id`), so re-running discovery
updates the same row each cycle: as a new edition is announced, today's
"upcoming" rolls into "prior" and the freshly announced dates become "upcoming".

Keeping prior and upcoming side by side lets the table show last year's dates as
a reference even before an organizer has published next year's schedule, which is
common many months out.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date
from enum import Enum
from typing import List, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from conference_agent.deadline_time import (
    KINDS,
    format_deadline_time,
    normalize_time,
    normalize_timezone,
    parse_legacy_deadline_time,
)


class ConferenceSize(str, Enum):
    """Size bucket of a conference, an objective proxy for prominence.

    A controlled value (rather than free text) so table views and queries can
    filter and color consistently. Unlike a subjective reputation judgment, the
    bucket is *derived deterministically* from a sourced attendance figure (see
    :func:`size_for_attendance`), so it is a fact that can be cited rather than an
    opinion. Example: RSNA (~45,000 attendees) is ``massive``.
    """

    MASSIVE = "massive"
    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


# Attendance thresholds (inclusive lower bounds) that bucket a conference into a
# size. Centralized so the rule is changed in one place and applied identically
# on read (the ``Conference.size`` property) and on write (the stored ``size``
# column). A meeting with >= MASSIVE attendees is "massive"; >= LARGE is "large";
# >= MEDIUM is "medium"; fewer is "small"; an unknown attendance yields no size.
MASSIVE_ATTENDANCE_THRESHOLD = 10_000
LARGE_ATTENDANCE_THRESHOLD = 1_000
MEDIUM_ATTENDANCE_THRESHOLD = 250


def size_for_attendance(attendance: "int | None") -> "ConferenceSize | None":
    """Bucket an attendance figure into a :class:`ConferenceSize`.

    The single, deterministic size rule: ``None`` in, ``None`` out (size is left
    blank when attendance is unknown rather than guessed).
    """
    if attendance is None:
        return None
    if attendance >= MASSIVE_ATTENDANCE_THRESHOLD:
        return ConferenceSize.MASSIVE
    if attendance >= LARGE_ATTENDANCE_THRESHOLD:
        return ConferenceSize.LARGE
    if attendance >= MEDIUM_ATTENDANCE_THRESHOLD:
        return ConferenceSize.MEDIUM
    return ConferenceSize.SMALL


class RemoteOption(str, Enum):
    """Whether a conference can be attended remotely (``None`` when not known)."""

    IN_PERSON = "in-person"
    VIRTUAL = "virtual"
    HYBRID = "hybrid"


# Subcategories are stored as a single column (a comma-joined string) but modeled
# as a list, since one conference can belong to several fields (e.g. SPR is both
# radiology and pediatrics; MICCAI is radiology and machine learning). This helper
# tokenizes any accepted form -- a list, or a delimited string -- into a clean,
# lowercased, de-duplicated list, so the model, the database, and the refresh
# policy all split tags the same way.
def _split_tags(value: "str | list | tuple | None") -> List[str]:
    """Tokenize a list/tuple or ``,``/``;``-delimited string into clean tags.

    Lowercased, stripped, de-duplicated, and order-preserving. Shared by the
    subcategory and format normalizers so every tag column splits identically.
    """
    if value is None:
        parts: List[str] = []
    elif isinstance(value, str):
        parts = re.split(r"[;,]", value)
    else:
        parts = [p for item in value for p in re.split(r"[;,]", str(item))]
    seen: set[str] = set()
    out: List[str] = []
    for part in parts:
        tag = part.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def normalize_subcategories(value: "str | list | tuple | None") -> List[str]:
    """Normalize subcategory tags to a lowercased, de-duplicated, ordered list."""
    return _split_tags(value)


# The ten suggested top-level categories. A conference's category is an input
# like its subcategories (multi-valued, one or more per series); these are the
# canonical values offered in pickers and listed first when present, but any
# other category is accepted as entered.
CATEGORIES = (
    "humanities",
    "social science",
    "medicine",
    "biology",
    "chemistry",
    "physics",
    "mathematics",
    "stats",
    "computer science",
    "artificial intelligence",
)


def normalize_categories(value: "str | list | tuple | None") -> List[str]:
    """Normalize category tags: lowercased and de-duplicated, with the
    :data:`CATEGORIES` values first in canonical order, then any custom ones in
    the order given."""
    tags = _split_tags(value)
    return [c for c in CATEGORIES if c in tags] + [t for t in tags if t not in CATEGORIES]


# The submission / presentation formats a conference offers. Unlike the free-text
# subcategory tags, this is a small controlled vocabulary: a meeting may invite a
# short abstract, a full paper / manuscript, a poster presentation, and/or an oral
# (podium) presentation -- often several at once. Listed here in the canonical
# display order so the column reads consistently regardless of input order.
CONFERENCE_FORMATS = ("abstract", "paper", "poster", "oral")


def normalize_formats(value: "str | list | tuple | None") -> List[str]:
    """Normalize submission/presentation formats to a canonical-ordered list.

    Accepts a list or a delimited string, lowercases and de-duplicates the tokens
    (reusing :func:`_split_tags`), then keeps only the recognized formats in
    :data:`CONFERENCE_FORMATS` and returns them in that canonical order. Tokens
    outside the vocabulary are dropped, so the column stays clean.
    """
    present = set(_split_tags(value))
    return [fmt for fmt in CONFERENCE_FORMATS if fmt in present]


# Published computer-science venue rankings, filled from the official lists by
# ``scripts/fill_rankings.py`` (never researched by discovery). CCF is the China
# Computer Federation's recommended list (A / B / C); CORE is the ICORE
# conference ranking (A* / A / B / C). CORE's other labels (National: <country>,
# Regional, Australasian B/C, Unranked, Journal Published) are not ranks on the
# A*-C scale, so they are stored as no value.
CCF_RANKS = ("A", "B", "C")
CORE_RANKS = ("A*", "A", "B", "C")
CCF_EDITION = "CCF 2022 (7th edition, with the 2024 name revisions)"
CORE_EDITION = "ICORE2026"
_CORE_NON_RANKS = re.compile(
    r"^(national|regional|australasian|unranked|not ranked|journal published|multiconference|tbr|none|-)",
    re.IGNORECASE,
)


def _normalize_rank(value, vocabulary: tuple, label: str, non_ranks=None) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if non_ranks is not None and non_ranks.match(text):
        return None
    # Accept "CCF-A", "CCF A", "CORE A*", "Rank A", "a*", "A类".
    core = re.sub(rf"^(?:{label}|rank)[\s:_-]*", "", text, flags=re.IGNORECASE)
    core = core.removesuffix("类").strip().upper()
    if core in vocabulary:
        return core
    raise ValueError(f"{label.upper()} rank must be one of {', '.join(vocabulary)}, got {value!r}")


def normalize_ccf_rank(value) -> Optional[str]:
    """A CCF rank (``A`` / ``B`` / ``C``), or ``None`` when blank.

    Raises ``ValueError`` for any other value.
    """
    return _normalize_rank(value, CCF_RANKS, "ccf")


def normalize_core_rank(value) -> Optional[str]:
    """A CORE rank (``A*`` / ``A`` / ``B`` / ``C``), or ``None`` when blank or a
    CORE label off that scale (National, Regional, Australasian, Unranked, ...).

    Raises ``ValueError`` for any other value.
    """
    return _normalize_rank(value, CORE_RANKS, "core", _CORE_NON_RANKS)


# The archival answer as entered and displayed (stored as a nullable boolean).
ARCHIVAL_CHOICES = ("yes", "no")
_ARCHIVAL_TRUE = ("yes", "y", "true", "t", "1", "archival")
_ARCHIVAL_FALSE = ("no", "n", "false", "f", "0", "non-archival", "nonarchival", "non archival")


def normalize_archival(value) -> Optional[bool]:
    """Whether a series publishes archival proceedings: ``True`` / ``False``, or
    ``None`` when blank or unknown.

    Accepts booleans, ``1`` / ``0``, and yes / no / true / false spellings
    (also ``archival`` / ``non-archival``); raises ``ValueError`` otherwise.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "unknown", "none", "null", "-", "?"):
        return None
    if text in _ARCHIVAL_TRUE:
        return True
    if text in _ARCHIVAL_FALSE:
        return False
    raise ValueError(f"archival must be yes or no, got {value!r}")


def normalize_guidelines(value) -> Optional[str]:
    """Clean submission-guideline text: ``\\n`` line endings, no trailing spaces
    on a line, no blank lines at either end, and runs of blank lines collapsed
    to one. ``None`` for blank input."""
    if value is None:
        return None
    lines = [line.rstrip() for line in str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


# --- Acceptance rates ---------------------------------------------------------
#
# A series' published acceptance statistics, one line per year, most recent
# first:
#
#     2026 | 4167/23680 | https://csconfstats.xoveexu.com/
#     2025 | 23.4% | https://aaai.org/...
#
# The middle part is accepted/submitted counts (the rate is computed from them)
# or a percentage when only the rate is published; the source URL is optional.
# Filled from ccfddl's list by ``scripts/fill_acceptance.py`` or by hand;
# discovery never researches it.
_ACCEPTANCE_COUNTS_RE = re.compile(r"^(\d[\d,]*)\s*/\s*(\d[\d,]*)$")
_ACCEPTANCE_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%$")


def _acceptance_entry(year, accepted=None, submitted=None, rate=None, source=None) -> dict:
    """One validated acceptance entry. ``rate`` is a percentage (0-100), or a
    fraction (0-1) when it comes with no ``%``. Raises ``ValueError``."""
    try:
        year = int(str(year).strip())
    except ValueError:
        raise ValueError(f"acceptance year must be a four-digit year, got {year!r}") from None
    if not 1900 <= year <= date.today().year + 1:
        raise ValueError(f"acceptance year {year} is out of range")

    def _count(value):
        if value in (None, ""):
            return None
        try:
            return int(str(value).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"acceptance count must be a whole number, got {value!r}") from None

    accepted, submitted = _count(accepted), _count(submitted)
    if (accepted is None) != (submitted is None):
        raise ValueError(f"acceptance {year}: give both accepted and submitted counts, or neither")
    if accepted is not None:
        if submitted <= 0 or accepted > submitted:
            raise ValueError(
                f"acceptance {year}: {accepted} accepted of {submitted} submitted is not a valid rate"
            )
        rate = 100 * accepted / submitted
    elif rate in (None, ""):
        raise ValueError(f"acceptance {year}: give accepted/submitted counts or a rate")
    else:
        text = str(rate).strip()
        percent = text.endswith("%")
        try:
            rate = float(text.rstrip("%").strip())
        except ValueError:
            raise ValueError(f"acceptance {year}: unreadable rate {text!r}") from None
        if not percent and rate <= 1:
            rate *= 100
        if not 0 < rate <= 100:
            raise ValueError(f"acceptance {year}: rate {text!r} is not between 0 and 100%")
    source = str(source).strip() if source not in (None, "") else None
    return {
        "year": year, "accepted": accepted, "submitted": submitted,
        "rate": round(rate, 1), "source": source,
    }


def parse_acceptance_rates(value) -> List[dict]:
    """Acceptance entries as ``{year, accepted, submitted, rate, source}`` dicts,
    most recent year first, one per year (the first given for a year wins).

    ``value`` is stored text (one ``YEAR | ACCEPTED/SUBMITTED | SOURCE`` or
    ``YEAR | RATE% | SOURCE`` line each; ``;`` also separates entries, and
    ``YEAR: ...`` is accepted for ``YEAR | ...``), or a list of such strings or
    of dicts (ccfddl's shape: ``year``, ``accepted``, ``submitted``, ``rate`` as
    a fraction, ``source``). Raises ``ValueError`` on an unreadable entry.
    """
    if value in (None, "", []):
        return []
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        value = [part for part in re.split(r"[;\n]", value) if part.strip()]
    entries: List[dict] = []
    for item in value:
        if isinstance(item, dict):
            entry = _acceptance_entry(
                item.get("year"), item.get("accepted"), item.get("submitted"),
                item.get("rate"), item.get("source"),
            )
        else:
            text = str(item).strip()
            m = re.match(r"^(\d{4})\s*[|:]\s*(.+)$", text)
            if not m:
                raise ValueError(
                    f"acceptance entry {text!r}: use 'YEAR | ACCEPTED/SUBMITTED | SOURCE' "
                    "or 'YEAR | RATE% | SOURCE'"
                )
            parts = [p.strip() for p in m.group(2).split("|")]
            figure, source = parts[0], " | ".join(parts[1:]) or None
            counts = _ACCEPTANCE_COUNTS_RE.match(figure)
            if counts:
                entry = _acceptance_entry(m.group(1), counts.group(1), counts.group(2), source=source)
            elif _ACCEPTANCE_PERCENT_RE.match(figure):
                entry = _acceptance_entry(m.group(1), rate=figure, source=source)
            else:
                raise ValueError(
                    f"acceptance entry {text!r}: give ACCEPTED/SUBMITTED counts or a RATE%"
                )
        entries.append(entry)
    seen: set = set()
    unique = []
    for entry in entries:
        if entry["year"] not in seen:
            seen.add(entry["year"])
            unique.append(entry)
    return sorted(unique, key=lambda e: e["year"], reverse=True)


def format_acceptance_rates(entries) -> Optional[str]:
    """The stored text for parsed acceptance entries (``None`` when empty)."""
    lines = []
    for e in entries:
        figure = (
            f"{e['accepted']}/{e['submitted']}" if e.get("accepted") is not None
            else f"{e['rate']:g}%"
        )
        lines.append(" | ".join(str(p) for p in (e["year"], figure, e.get("source")) if p))
    return "\n".join(lines) or None


def normalize_acceptance_rates(value) -> Optional[str]:
    """Canonical acceptance-rate text (see :func:`parse_acceptance_rates`)."""
    return format_acceptance_rates(parse_acceptance_rates(value))


def merge_acceptance_rates(stored, new) -> Optional[str]:
    """``new`` entries over ``stored`` ones: a year in ``new`` replaces the
    stored entry for that year; other stored years are kept."""
    return format_acceptance_rates(
        parse_acceptance_rates([*parse_acceptance_rates(new), *parse_acceptance_rates(stored)])
    )


def latest_acceptance(value) -> Optional[dict]:
    """The most recent acceptance entry of ``value``, or ``None``."""
    try:
        entries = parse_acceptance_rates(value)
    except ValueError:
        return None
    return entries[0] if entries else None


# --- Registration -------------------------------------------------------------
#
# An edition's registration is stored as text, one "Label: value" line per
# published item, in this order, with only the items the meeting published:
#
#     Opens: 2026-07-23
#     Early bird deadline: 2026-08-10
#     Final deadline: 2026-11-24
#     Refund deadline: 2026-10-30
#     Notes: Advance rate through 2026-09-21.
#
# A date is ISO ``YYYY-MM-DD``, or ``YYYY-MM`` when only the month is published;
# a value that is not a date (e.g. "Fall 2026") is kept as written. Notes holds
# only what the four dates cannot say. Text in any other shape (the earlier
# free-text windows) is kept as given until it is researched again.
REGISTRATION_FIELDS = (
    ("opens", "Opens"),
    ("early_bird_deadline", "Early bird deadline"),
    ("final_deadline", "Final deadline"),
    ("refund_deadline", "Refund deadline"),
    ("notes", "Notes"),
)
_REGISTRATION_LABELS = {label.lower(): key for key, label in REGISTRATION_FIELDS}
# Other spellings accepted on input (an ``add`` value, a form entry, an agent).
_REGISTRATION_LABELS.update({
    "registration opens": "opens",
    "open": "opens",
    "early bird": "early_bird_deadline",
    "early-bird deadline": "early_bird_deadline",
    "earlybird deadline": "early_bird_deadline",
    "early registration deadline": "early_bird_deadline",
    "registration deadline": "final_deadline",
    "registration closes": "final_deadline",
    "closes": "final_deadline",
    "refund": "refund_deadline",
    "cancellation deadline": "refund_deadline",
    "note": "notes",
})
_REGISTRATION_LABEL_RE = re.compile(
    r"^\s*(" + "|".join(sorted((re.escape(k) for k in _REGISTRATION_LABELS), key=len, reverse=True))
    + r")\s*:\s*(.*)$",
    re.IGNORECASE,
)
# Split "Opens: ...; Final deadline: ..." (one CSV cell) at each label.
_REGISTRATION_SPLIT_RE = re.compile(
    r";\s*(?=(?:" + "|".join(sorted((re.escape(k) for k in _REGISTRATION_LABELS), key=len, reverse=True))
    + r")\s*:)",
    re.IGNORECASE,
)
_MONTHS = {
    name.lower(): i
    for i in range(1, 13)
    for name in (calendar.month_name[i], calendar.month_abbr[i])
}
_MONTHS["sept"] = 9


def _registration_date(value: str) -> str:
    """A registration date as ISO ``YYYY-MM-DD`` (or ``YYYY-MM`` for a month);
    a value that is not a single date is returned as written."""
    text = value.strip().rstrip(".")
    text = re.sub(r"^(?:on|by|through|until|thru)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+", "", text, flags=re.IGNORECASE)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", text)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3]) if m[3] else None
    else:
        word = r"([A-Za-z]+)\.?"
        patterns = (
            (rf"{word}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", ("m", "d", "y")),
            (rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+{word},?\s+(\d{{4}})", ("d", "m", "y")),
            (rf"{word},?\s+(\d{{4}})", ("m", "y")),
        )
        for pattern, order in patterns:
            m = re.fullmatch(pattern, text)
            if m:
                parts = dict(zip(order, m.groups()))
                mo = _MONTHS.get(parts["m"].lower())
                y, d = int(parts["y"]), int(parts["d"]) if "d" in parts else None
                break
        else:
            return value.strip()
    if not mo or not 1 <= mo <= 12:
        return value.strip()
    if d is None:
        return f"{y:04d}-{mo:02d}"
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return value.strip()


def parse_registration(value) -> Optional[dict]:
    """The registration items of ``value`` as ``{key: value}`` (keys from
    :data:`REGISTRATION_FIELDS`), or ``None`` when it is not in the line format
    (blank, or earlier free text). A dict is read by key."""
    if value is None:
        return None
    if isinstance(value, dict):
        items = {}
        for raw_key, raw in value.items():
            key = _REGISTRATION_LABELS.get(str(raw_key).replace("_", " ").strip().lower(), raw_key)
            if key in dict(REGISTRATION_FIELDS) and raw not in (None, "") and str(raw).strip():
                items[key] = str(raw).strip()
    else:
        text = str(value).replace("\r\n", "\n").replace("\r", "\n")
        lines = [
            part.strip()
            for line in text.split("\n")
            for part in _REGISTRATION_SPLIT_RE.split(line)
            if part.strip()
        ]
        if not lines:
            return None
        items = {}
        canonical = set()  # keys written under their own label
        for line in lines:
            m = _REGISTRATION_LABEL_RE.match(line)
            if not m:
                return None
            key = _REGISTRATION_LABELS[m[1].lower()]
            if m[2].strip() and key not in items:
                items[key] = m[2].strip()
                if m[1].lower() == dict(REGISTRATION_FIELDS)[key].lower():
                    canonical.add(key)
    out = {}
    for key, _ in REGISTRATION_FIELDS:
        raw = items.get(key)
        if raw is None or raw.strip().lower() in ("", "n/a", "none", "unknown", "tba", "tbd"):
            continue
        if key == "notes":
            out[key] = " ".join(raw.split())
            continue
        out[key] = _registration_date(raw)
        # A date item that is not a date is kept as written ("Opens: Fall
        # 2026") only under its own label: under another spelling it is the
        # earlier free text (e.g. "Early bird: Jan 5 - Mar 1"), as is one that
        # itself holds several items ("...; Advance: through Sep 21").
        if not isinstance(value, dict) and not re.match(r"\d{4}-\d{2}", out[key]) and (
            key not in canonical or re.search(r"[:;]", raw)
        ):
            return None
    return out


def format_registration(items: dict) -> Optional[str]:
    """The stored text for parsed registration items (see :data:`REGISTRATION_FIELDS`)."""
    lines = [f"{label}: {items[key]}" for key, label in REGISTRATION_FIELDS if items.get(key)]
    return "\n".join(lines) or None


def normalize_registration(value) -> Optional[str]:
    """Canonical registration text: the line format with ISO dates, in field
    order, for input in that format (any label spelling, ``;`` or newline
    separated) or a dict; other text is kept as given (whitespace cleaned).
    ``None`` for blank input."""
    items = parse_registration(value)
    if items is not None:
        return format_registration(items)
    if value is None or isinstance(value, dict):
        return None
    return normalize_guidelines(value)


def is_structured_registration(value) -> bool:
    """True when ``value`` is non-blank registration text in the line format."""
    return bool(value) and bool(parse_registration(value))


# --- Cost ---------------------------------------------------------------------
#
# An edition's registration cost is stored as text, one "Tier: price" line per
# published tier, in this order, with only the tiers the meeting published:
#
#     Early bird: $865 member / $1,175 non-member
#     Regular: $1,015 member / $1,365 non-member
#     Late: $1,099 member / $1,499 non-member
#     Trainee: $100
#     Virtual: $575 member / $1,125 non-member
#     Notes: Fellows, residents, and students free.
#
# Amounts are written with a currency symbol ("$1,095", "€710", "A$2,035",
# "CA$1,200"), whatever spelling the input used ("EUR 710", "710 EUR"). Each
# tier names the standard (attending professional) rate, member first then
# non-member when they differ. Text in any other shape (the earlier free-text
# summaries) is kept as given until it is researched again.
#
# Sources reword the same fees from run to run, so a cost is compared by its
# amounts (:func:`same_cost`), not its text: a write whose amounts match the
# stored value keeps the stored text, and subscribers are not emailed.
COST_TIERS = (
    ("early_bird", "Early bird"),
    ("regular", "Regular"),
    ("late", "Late"),
    ("trainee", "Trainee"),
    ("virtual", "Virtual"),
    ("notes", "Notes"),
)
_COST_LABELS = {label.lower(): key for key, label in COST_TIERS}
_COST_LABELS.update({
    "early bird": "early_bird",
    "early-bird": "early_bird",
    "earlybird": "early_bird",
    "early": "early_bird",
    "early registration": "early_bird",
    "standard": "regular",
    "full": "regular",
    "onsite": "late",
    "on-site": "late",
    "late / onsite": "late",
    "last minute": "late",
    "student": "trainee",
    "students": "trainee",
    "trainees": "trainee",
    "resident": "trainee",
    "online": "virtual",
    "virtual only": "virtual",
    "note": "notes",
})
_COST_LABEL_RE = re.compile(
    r"^\s*(" + "|".join(sorted((re.escape(k) for k in _COST_LABELS), key=len, reverse=True))
    + r")\s*:\s*(.*)$",
    re.IGNORECASE,
)
_COST_SPLIT_RE = re.compile(
    r";\s*(?=(?:" + "|".join(sorted((re.escape(k) for k in _COST_LABELS), key=len, reverse=True))
    + r")\s*:)",
    re.IGNORECASE,
)
# Currency spellings -> the canonical prefix.
_CURRENCY_PREFIX = {
    "$": "$", "us$": "$", "usd": "$",
    "€": "€", "eur": "€",
    "£": "£", "gbp": "£",
    "¥": "¥", "jpy": "¥",
    "cn¥": "CN¥", "cny": "CN¥", "rmb": "CN¥",
    "a$": "A$", "au$": "A$", "aud": "A$",
    "ca$": "CA$", "c$": "CA$", "cad": "CA$",
    "nz$": "NZ$", "nzd": "NZ$",
    "hk$": "HK$", "hkd": "HK$",
    "s$": "S$", "sgd": "S$",
    "₹": "₹", "inr": "₹",
    "₩": "₩", "krw": "₩",
    "chf": "CHF ", "sek": "SEK ", "dkk": "DKK ", "nok": "NOK ", "brl": "R$", "r$": "R$",
}
_CURRENCY_ALT = "|".join(
    sorted((re.escape(k) for k in _CURRENCY_PREFIX), key=len, reverse=True)
)
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# An amount with its currency before ("$1,095", "EUR 710", "AUD $2,035") or
# after ("710 EUR", "710€"), optionally a range ("€80-€400", "EUR 90-120").
_AMOUNT_RE = re.compile(
    rf"(?<![A-Za-z0-9$])(?:(?P<pre>{_CURRENCY_ALT})(?:\s*\$)?\s?(?P<n1>{_NUMBER})"
    rf"|(?P<n2>{_NUMBER})\s?(?P<post>{_CURRENCY_ALT})(?![A-Za-z]))"
    rf"(?:\s?[-–]\s?(?:(?:{_CURRENCY_ALT})(?:\s*\$)?\s?)?(?P<n3>{_NUMBER})(?![\d,]))?",
    re.IGNORECASE,
)
_FREE_RE = re.compile(r"\b(?:free|complimentary|no (?:fee|charge))\b", re.IGNORECASE)


def _amount_number(text: str) -> float:
    return float(text.replace(",", ""))


def _amount_text(prefix: str, number: float) -> str:
    shown = f"{number:,.0f}" if number == int(number) else f"{number:,.2f}"
    return f"{prefix}{shown}"


def cost_amounts(value) -> list:
    """The amounts in cost text as sorted ``(currency prefix, number)`` pairs;
    a free tier counts as ``("", 0)``, as does any zero amount."""
    out = []
    for m in _AMOUNT_RE.finditer(str(value or "")):
        prefix = _CURRENCY_PREFIX[(m["pre"] or m["post"]).lower()]
        for n in (m["n1"] or m["n2"], m["n3"]):
            if n:
                number = _amount_number(n)
                out.append(("", 0.0) if number == 0 else (prefix, number))
    out += [("", 0.0)] * len(_FREE_RE.findall(str(value or "")))
    return sorted(out)


def _canonical_price(text: str) -> str:
    """A tier's price text with each amount spelled canonically and the member /
    non-member words in one spelling."""
    def amount(m):
        prefix = _CURRENCY_PREFIX[(m["pre"] or m["post"]).lower()]
        shown = _amount_text(prefix, _amount_number(m["n1"] or m["n2"]))
        if m["n3"]:
            shown += "–" + _amount_text(prefix, _amount_number(m["n3"]))
        return shown

    text = _AMOUNT_RE.sub(amount, " ".join(text.split()))
    text = re.sub(r"\bnon[- ]?members?\b", "non-member", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!-)\bmembers?\b", "member", text, flags=re.IGNORECASE)
    return text.rstrip(".")


def parse_cost(value) -> Optional[dict]:
    """The cost tiers of ``value`` as ``{key: price}`` (keys from
    :data:`COST_TIERS`), or ``None`` when it is not in the line format (blank,
    or earlier free text). A dict is read by key."""
    if value is None:
        return None
    if isinstance(value, dict):
        items = {}
        for raw_key, raw in value.items():
            key = _COST_LABELS.get(str(raw_key).replace("_", " ").strip().lower(), raw_key)
            if key in dict(COST_TIERS) and raw not in (None, "") and str(raw).strip():
                items[key] = str(raw).strip()
    else:
        text = str(value).replace("\r\n", "\n").replace("\r", "\n")
        lines = [
            part.strip()
            for line in text.split("\n")
            for part in _COST_SPLIT_RE.split(line)
            if part.strip()
        ]
        if not lines:
            return None
        items = {}
        for line in lines:
            m = _COST_LABEL_RE.match(line)
            # A tier holding several items ("Early bird: $1,049; students
            # $379") is the earlier free text.
            if not m or (_COST_LABELS[m[1].lower()] != "notes" and ";" in m[2]):
                return None
            key = _COST_LABELS[m[1].lower()]
            if m[2].strip() and key not in items:
                items[key] = m[2].strip()
    out = {}
    for key, _ in COST_TIERS:
        raw = items.get(key)
        if raw is None or raw.strip().lower() in ("", "n/a", "none", "unknown", "tba", "tbd"):
            continue
        out[key] = " ".join(raw.split()) if key == "notes" else _canonical_price(raw)
    return out


def format_cost(items: dict) -> Optional[str]:
    """The stored text for parsed cost tiers (see :data:`COST_TIERS`)."""
    lines = [f"{label}: {items[key]}" for key, label in COST_TIERS if items.get(key)]
    return "\n".join(lines) or None


def normalize_cost(value) -> Optional[str]:
    """Canonical cost text: the tier line format with canonical amounts, in
    tier order, for input in that format (any label spelling, ``;`` or newline
    separated) or a dict; other text is kept as given (whitespace cleaned).
    ``None`` for blank input."""
    items = parse_cost(value)
    if items is not None:
        return format_cost(items)
    if value is None or isinstance(value, dict):
        return None
    return " ".join(str(value).split()) or None


def is_structured_cost(value) -> bool:
    """True when ``value`` is non-blank cost text in the tier line format."""
    return bool(value) and bool(parse_cost(value))


def _cost_words(text: str) -> list:
    return re.findall(r"[a-z0-9]+", text.lower())


def same_cost(a, b) -> bool:
    """Whether two cost texts state the same fees, ignoring wording.

    Two tiered values are compared tier by tier, each by its amounts (or, for a
    tier with no amount, its words; a Notes line by its amounts alone). Free text
    is compared by all of its amounts. A tiered value that restates free text is
    the same cost when every amount it lists appears in the free text (the
    tiers drop rates the free text also listed, such as industry rates).
    """
    a, b = (a or "").strip() or None, (b or "").strip() or None
    if a is None or b is None:
        return a is b
    pa, pb = parse_cost(a), parse_cost(b)
    if pa and pb:
        def key(items):
            out = {}
            for tier, text in items.items():
                amounts = cost_amounts(text)
                if tier == "notes":
                    if amounts:
                        out[tier] = amounts
                else:
                    out[tier] = amounts or _cost_words(text)
            return out
        return key(pa) == key(pb)
    aa, ab = cost_amounts(a), cost_amounts(b)
    if not aa and not ab:
        return _cost_words(a) == _cost_words(b)
    if bool(pa) != bool(pb):
        tiered, free = (aa, ab) if pa else (ab, aa)
        return bool(tiered) and set(tiered) <= set(free)
    return aa == ab


def name_id(name: str) -> str:
    """The record id for a conference name: a lowercase, hyphenated ASCII slug.

    Series are indexed by name, so this is also the name comparison used to match
    rows: case, accents, punctuation, and spacing do not distinguish two names
    (``"IDWeek"`` and ``"idweek"`` are one series). The slug is the id in
    calendar event UIDs, subscription keys, and the ``/c/<id>/`` page URLs.
    """
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


# The dated kinds that have Upcoming / Past columns backed by a per-kind
# history (``<kind>_deadline_history``): the three submission deadlines plus
# the decision (notification) date. Only the three in ``KINDS`` have a time of
# day, extensions, a derived month, and count as deadlines (reminders, "Hide
# past deadlines"); the decision date is when authors hear back.
HISTORY_KINDS = (*KINDS, "decision")

# Per-edition fields, as the suffix after ``prior_`` / ``upcoming_``.
EDITION_SUFFIXES = (
    "abstract_deadline",
    "late_abstract_deadline",
    "paper_deadline",
    "decision_deadline",
    "start_date",
    "end_date",
    "registration",
    "location",
    "cost",
)


class Conference(BaseModel):
    """A recurring conference series with its prior and upcoming editions."""

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _collapse_duplicate_edition(cls, data):
        """Merge a prior edition that duplicates the upcoming one.

        Two editions cannot start on the same day, so a record whose prior and
        upcoming start dates are equal holds one edition twice (an extraction
        error). The prior slots fill any upcoming blanks and are then cleared;
        :func:`database.roll_past_editions` later moves the edition into the prior
        slots once it is over.
        """
        if not isinstance(data, dict):
            return data
        prior, upcoming = data.get("prior_start_date"), data.get("upcoming_start_date")
        if prior in (None, "") or str(prior) != str(upcoming):
            return data
        data = dict(data)
        for edition in ("prior", "upcoming"):  # fold the legacy alias in first
            alias = data.pop(f"{edition}_registration_date", None)
            data.setdefault(f"{edition}_registration", alias)
        for suffix in ("location", "cost"):  # likewise the single-value keys
            if data.get(f"upcoming_{suffix}") in (None, ""):
                data[f"upcoming_{suffix}"] = data.pop(suffix, None)
        for suffix in EDITION_SUFFIXES:
            value = data.pop(f"prior_{suffix}", None)
            if data.get(f"upcoming_{suffix}") in (None, "") and value not in (None, ""):
                data[f"upcoming_{suffix}"] = value
        return data

    # --- Identity ----------------------------------------------------------
    acronym: str = Field(..., description="Short name, e.g. 'RSNA'")
    name: str = Field(..., description="Full conference name (the series' index)")

    @field_validator("name")
    @classmethod
    def _name_has_id(cls, value: str) -> str:
        value = " ".join(value.split())
        if not name_id(value):
            raise ValueError("name must contain at least one ASCII letter or digit")
        return value
    # One conference can carry several subcategory tags (e.g. SPR -> radiology +
    # pediatrics). Accepts either a list or a comma/semicolon-delimited string (and
    # the singular ``subcategory`` key) on input; ``subcategory`` below exposes the
    # joined string for display and storage.
    subcategories: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subcategories", "subcategory"),
        description="Specific field(s), e.g. ['radiology', 'machine learning']",
    )
    # The broad bucket(s), set independently of the subcategories: usually from
    # :data:`CATEGORIES` (e.g. MICCAI -> medicine + artificial intelligence), but
    # a custom category is kept as entered. ``category`` joins them.
    categories: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("categories", "category"),
        description="Broad categories, e.g. ['medicine', 'artificial intelligence']",
    )

    @field_validator("subcategories", mode="before")
    @classmethod
    def _normalize_subcategories(cls, value):
        return normalize_subcategories(value)

    @field_validator("categories", mode="before")
    @classmethod
    def _normalize_categories(cls, value):
        return normalize_categories(value)

    # --- Prior (most recent completed) edition -----------------------------
    prior_abstract_deadline: Optional[date] = Field(
        None, description="Abstract submission deadline of the most recent edition"
    )
    prior_late_abstract_deadline: Optional[date] = Field(
        None,
        description=(
            "Second, later abstract deadline of the most recent edition -- a "
            "poster-only or late-breaking window (see "
            ":attr:`upcoming_late_abstract_deadline`). Blank when the edition "
            "published only one abstract deadline."
        ),
    )
    prior_paper_deadline: Optional[date] = Field(
        None, description="Full paper / manuscript deadline of the most recent edition"
    )
    prior_decision_deadline: Optional[date] = Field(
        None,
        description="Acceptance decision (author notification) date of the most recent edition",
        validation_alias=AliasChoices("prior_decision_deadline", "prior_decision_date"),
    )
    prior_start_date: Optional[date] = Field(
        None, description="First day of the most recent edition"
    )
    prior_end_date: Optional[date] = Field(
        None, description="Last day of the most recent edition"
    )
    prior_registration: Optional[str] = Field(
        None,
        description=(
            "Registration dates of the most recent edition, one 'Label: value' line per "
            "published item: 'Opens', 'Early bird deadline', 'Final deadline', "
            "'Refund deadline' (dates as YYYY-MM-DD, or YYYY-MM for a month), and "
            "'Notes' only for what the dates cannot say. Blank when none is published."
        ),
        validation_alias=AliasChoices("prior_registration", "prior_registration_date"),
    )
    prior_location: Optional[str] = Field(
        None, description="Host city / venue of the most recent edition, e.g. 'Chicago, IL'"
    )
    prior_cost: Optional[str] = Field(
        None,
        description=(
            "Registration cost of the most recent edition, one 'Tier: price' line per "
            "published tier: 'Early bird', 'Regular', 'Late', 'Trainee', 'Virtual', "
            "and 'Notes' only for what the tiers cannot say (see COST_TIERS)."
        ),
    )

    # --- Upcoming edition --------------------------------------------------
    upcoming_abstract_deadline: Optional[date] = Field(
        None, description="Abstract submission deadline of the upcoming edition"
    )
    upcoming_late_abstract_deadline: Optional[date] = Field(
        None,
        description=(
            "Second, later abstract deadline of the upcoming edition. Many series "
            "publish two: a main deadline and a later window that is narrower in "
            "scope. Two shapes recur -- a poster-only deadline when the main one "
            "is talk-only (CSHL Biological Data Science: talks Aug 28, posters "
            "Oct 1), and a late-breaking / late-poster round after the main call "
            "closes (ASHG, ISMB, RECOMB). Both go here; "
            "``upcoming_abstract_deadline`` always holds the earlier, primary "
            "deadline. Blank when the edition publishes only one."
        ),
    )
    upcoming_paper_deadline: Optional[date] = Field(
        None, description="Full paper / manuscript deadline of the upcoming edition"
    )
    upcoming_decision_deadline: Optional[date] = Field(
        None,
        description=(
            "Acceptance decision (author notification) date of the upcoming edition: "
            "when the main submission round's authors hear back"
        ),
        validation_alias=AliasChoices("upcoming_decision_deadline", "upcoming_decision_date"),
    )
    upcoming_start_date: Optional[date] = Field(
        None, description="First day of the upcoming edition"
    )
    upcoming_end_date: Optional[date] = Field(
        None, description="Last day of the upcoming edition"
    )
    upcoming_registration: Optional[str] = Field(
        None,
        description=(
            "Registration dates of the upcoming edition, one 'Label: value' line per "
            "published item: 'Opens', 'Early bird deadline', 'Final deadline', "
            "'Refund deadline' (dates as YYYY-MM-DD, or YYYY-MM for a month), and "
            "'Notes' only for what the dates cannot say. Blank when none is published."
        ),
        validation_alias=AliasChoices("upcoming_registration", "upcoming_registration_date"),
    )
    # Location and cost belong to an edition (a series moves cities and changes
    # its fees). The legacy single ``location`` / ``cost`` input keys fill the
    # upcoming slot; the :attr:`location` / :attr:`cost` properties give the
    # displayed value (upcoming, else prior).
    upcoming_location: Optional[str] = Field(
        None,
        description="Host city / venue of the upcoming edition, e.g. 'Chicago, IL' or 'Vienna, Austria'",
        validation_alias=AliasChoices("upcoming_location", "location"),
    )
    upcoming_cost: Optional[str] = Field(
        None,
        description=(
            "Registration cost of the upcoming edition, one 'Tier: price' line per "
            "published tier, e.g. 'Early bird: $1,095 member / $1,395 non-member'"
        ),
        validation_alias=AliasChoices("upcoming_cost", "cost"),
    )

    # --- Logistics & classification ----------------------------------------
    # True when the series meets in the same place every edition (RSNA is always
    # in Chicago), so a location recorded for a prior edition still describes the
    # next one and the table shows it as current rather than as a past value.
    stable_location: bool = Field(
        False, description="Whether the series is held in the same location every edition"
    )
    url: Optional[str] = Field(None, description="Official conference website link")
    remote_option: Optional[RemoteOption] = Field(
        None, description="In-person / virtual / hybrid attendance option"
    )
    # Submission / presentation formats the conference offers (any of abstract,
    # paper, poster, oral). Like categories, accepts either a list or a
    # comma/semicolon-delimited string (and the singular ``format`` key) on input;
    # ``format`` below exposes the joined string for display and storage. Distinct
    # from ``remote_option`` (how you attend) -- this is how work is presented.
    formats: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("formats", "format"),
        description="Submission/presentation format(s) offered, any of: abstract, paper, poster, oral",
    )

    @field_validator("stable_location", mode="before")
    @classmethod
    def _parse_stable_location(cls, value):
        # Blank means "not stated", i.e. False; accept yes/no spellings from CSV.
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "yes", "y", "1"):
                return True
            if text in ("false", "no", "n", "0"):
                return False
        return value

    # Whether accepted submissions are published in archival proceedings (a
    # citable proceedings volume or journal, including a journal abstract
    # supplement). False means non-archival: the work may also be submitted
    # elsewhere. None means unknown; a survey that reports none keeps the
    # stored value.
    archival: Optional[bool] = Field(
        None,
        description=(
            "Whether accepted submissions are published in archival proceedings "
            "(a citable proceedings volume, or a journal / journal supplement): "
            "true, false (non-archival), or null when unknown"
        ),
    )

    @field_validator("archival", mode="before")
    @classmethod
    def _normalize_archival(cls, value):
        return normalize_archival(value)

    # Published CS venue rankings (see ``CCF_RANKS`` / ``CORE_RANKS``): set by
    # hand or by ``scripts/fill_rankings.py`` from the official lists, never by
    # discovery (a survey that reports none keeps the stored value).
    ccf_rank: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("ccf_rank", "ccf"),
        description="China Computer Federation recommended-list rank: A / B / C",
    )
    core_rank: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("core_rank", "core"),
        description="ICORE conference rank: A* / A / B / C",
    )

    @field_validator("ccf_rank", mode="before")
    @classmethod
    def _normalize_ccf(cls, value):
        return normalize_ccf_rank(value)

    @field_validator("core_rank", mode="before")
    @classmethod
    def _normalize_core(cls, value):
        return normalize_core_rank(value)

    @field_validator("remote_option", mode="before")
    @classmethod
    def _blank_unknown_remote(cls, value):
        # An unknown option is stored as no value, not as its own category.
        if isinstance(value, str) and value.strip().lower() in ("", "unknown"):
            return None
        return value

    @field_validator("formats", mode="before")
    @classmethod
    def _normalize_formats(cls, value):
        return normalize_formats(value)
    # Attendance is the objective input from which ``size`` is derived. The figure
    # is paired with the year it describes and the source it was taken from, so the
    # derived size is auditable rather than a bare assertion. The source URL is
    # provenance kept internal (it is not surfaced in the public table).
    attendance: Optional[int] = Field(
        None, description="Typical annual attendee count, e.g. 45000"
    )
    attendance_year: Optional[int] = Field(
        None, description="Year the attendance figure describes, e.g. 2025"
    )
    attendance_source: Optional[str] = Field(
        None, description="Source URL the attendance figure was taken from (internal provenance)"
    )
    # Published acceptance statistics, one line per year (see
    # ``parse_acceptance_rates``). Like the rankings, filled from a published
    # list (``scripts/fill_acceptance.py``) or by hand, never by discovery.
    acceptance_rates: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("acceptance_rates", "acceptance"),
        description="Acceptance statistics, one 'YEAR | ACCEPTED/SUBMITTED | SOURCE' "
        "(or 'YEAR | RATE% | SOURCE') line per year",
    )

    @field_validator("acceptance_rates", mode="before")
    @classmethod
    def _normalize_acceptance(cls, value):
        return normalize_acceptance_rates(value)

    notes: Optional[str] = Field(None, description="Free-form notes")
    # Submission requirements, per series (a call's rules rarely change between
    # editions): free text, a paragraph or "- " bullet lines, covering word /
    # character limits, required structure, figures, templates, and so on.
    # Researched by a dedicated pass (``discover.research_guidelines``), not by
    # the survey, which leaves them unset (``upsert_conferences`` then keeps the
    # stored text).
    abstract_guidelines: Optional[str] = Field(
        None,
        description="Abstract submission requirements (word/character limits, structure, ...), "
        "a paragraph or '- ' bullet lines",
    )
    paper_guidelines: Optional[str] = Field(
        None,
        description="Full-paper submission requirements (page limit, template, anonymization, ...), "
        "a paragraph or '- ' bullet lines",
    )

    @field_validator("prior_registration", "upcoming_registration", mode="before")
    @classmethod
    def _normalize_registration(cls, value):
        return normalize_registration(value)

    @field_validator("prior_cost", "upcoming_cost", mode="before")
    @classmethod
    def _normalize_cost(cls, value):
        return normalize_cost(value)

    @field_validator("abstract_guidelines", "paper_guidelines", mode="before")
    @classmethod
    def _clean_guidelines(cls, value):
        return normalize_guidelines(value)

    # Time of day and time zone each deadline closes, one pair per deadline kind.
    # These are the stored source of truth (not shown as columns); the table's
    # ``Deadline time`` text is *derived* from them (see :attr:`deadline_time`).
    # Times are canonical 24-hour ``HH:MM`` and zones are canonical codes
    # (``AoE``, ``ET``, ``CET``, ... or an IANA name), whatever form the input took
    # -- see ``deadline_time.py``. The deadline columns stay pure dates (sorting,
    # derived months, date comparisons, and all-day calendar events depend on
    # that), so a time rides along per kind. A zone with no time, or a time with no
    # zone, is kept as published.
    abstract_time: Optional[str] = Field(None, description="Abstract deadline time of day, 24-hour HH:MM")
    abstract_timezone: Optional[str] = Field(None, description="Zone of the abstract deadline time, e.g. 'AoE', 'ET'")
    late_abstract_time: Optional[str] = Field(None, description="Late abstract deadline time of day, 24-hour HH:MM")
    late_abstract_timezone: Optional[str] = Field(None, description="Zone of the late abstract deadline time")
    paper_time: Optional[str] = Field(None, description="Paper deadline time of day, 24-hour HH:MM")
    paper_timezone: Optional[str] = Field(None, description="Zone of the paper deadline time")

    @model_validator(mode="before")
    @classmethod
    def _structure_deadline_times(cls, data):
        """Accept the legacy free-text ``deadline_time`` as input.

        ``deadline_time`` is now derived, but older CSV/JSON files and table
        exports still carry it. When given, it is parsed into whichever of the six
        structured fields the input did not set explicitly (explicit fields win).
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        legacy = data.pop("deadline_time", None)
        if legacy and str(legacy).strip():
            dated = [
                k
                for k in KINDS
                if any(data.get(f"{ed}_{k}_deadline") for ed in ("prior", "upcoming"))
            ]
            for key, value in parse_legacy_deadline_time(str(legacy), dated).items():
                if data.get(key) in (None, ""):
                    data[key] = value
        return data

    @field_validator("abstract_time", "late_abstract_time", "paper_time", mode="before")
    @classmethod
    def _normalize_deadline_time(cls, value):
        return normalize_time(value) if value not in (None, "") else None

    @field_validator(
        "abstract_timezone", "late_abstract_timezone", "paper_timezone", mode="before"
    )
    @classmethod
    def _normalize_deadline_timezone(cls, value):
        return normalize_timezone(value) if value not in (None, "") else None

    @property
    def deadline_specs(self) -> dict:
        """``{kind: (time, timezone)}`` for the three deadline kinds."""
        return {
            k: (getattr(self, f"{k}_time"), getattr(self, f"{k}_timezone")) for k in KINDS
        }

    @property
    def deadline_time(self) -> Optional[str]:
        """The deadline time(s) as one display string, derived from the six fields.

        ``"11:59 PM ET"`` when every deadline the series has shares one time,
        otherwise one labeled line per deadline (``"abstract: 5:00 PM ET"`` /
        ``"paper: 11:59 PM AoE"``). ``None`` when no time is recorded. Always the
        12-hour form; the browser re-renders the structured fields in 24-hour on
        request.
        """
        dated = [
            k
            for k in KINDS
            if getattr(self, f"upcoming_{k}_deadline") or getattr(self, f"prior_{k}_deadline")
        ]
        return format_deadline_time(self.deadline_specs, dated)

    @property
    def id(self) -> str:
        """Record id for a series: the slug of its name (see :func:`name_id`)."""
        return name_id(self.name)

    @property
    def subcategory(self) -> str:
        """The subcategories as a single comma-joined string (for display / storage)."""
        return ", ".join(self.subcategories)

    @property
    def category(self) -> str:
        """The categories as a single comma-joined string (for display / storage)."""
        return ", ".join(self.categories)

    @property
    def format(self) -> str:
        """The formats as a single comma-joined string (for display / storage)."""
        return ", ".join(self.formats)

    @property
    def size(self) -> Optional[ConferenceSize]:
        """Size bucket, derived deterministically from :attr:`attendance`.

        A computed property, not a stored field: there is no hand-set label to
        defend, so the size is always exactly what the attendance figure implies
        (see :func:`size_for_attendance`). ``None`` when attendance is unknown.
        """
        return size_for_attendance(self.attendance)

    @property
    def attendance_display(self) -> Optional[str]:
        """Attendance formatted for display, e.g. ``"45,000 (2025)"``.

        The year the figure describes is appended in parentheses when known.
        ``None`` when no attendance figure is recorded.
        """
        if self.attendance is None:
            return None
        if self.attendance_year is not None:
            return f"{self.attendance:,} ({self.attendance_year})"
        return f"{self.attendance:,}"

    @property
    def acceptance_rate(self) -> Optional[float]:
        """The most recent year's acceptance rate, in percent (e.g. ``17.6``)."""
        latest = latest_acceptance(self.acceptance_rates)
        return latest["rate"] if latest else None

    @property
    def acceptance_display(self) -> Optional[str]:
        """The most recent acceptance rate for display, e.g. ``"17.6% (2026)"``."""
        latest = latest_acceptance(self.acceptance_rates)
        return f"{latest['rate']:g}% ({latest['year']})" if latest else None

    @property
    def upcoming_year(self) -> Optional[int]:
        """Year of the upcoming edition, if its start date is known."""
        return self.upcoming_start_date.year if self.upcoming_start_date else None

    @property
    def conference_month(self) -> Optional[int]:
        """Month (1-12) the conference is held, derived from its start date.

        Uses the upcoming edition's start date, falling back to the prior
        edition's -- the same date the table shows. Kept separate from the
        conference dates so rows can be sorted by season even when their years are
        offset (e.g. a meeting whose next edition is unannounced still sorts by the
        month of its most recent one).
        """
        start = self.upcoming_start_date or self.prior_start_date
        return start.month if start else None

    @property
    def conference_month_name(self) -> Optional[str]:
        """Full month name the conference is held in (e.g. ``"November"``)."""
        month = self.conference_month
        return calendar.month_name[month] if month else None

    @property
    def abstract_month(self) -> Optional[int]:
        """Month (1-12) the abstract is due, derived from the abstract deadline.

        Uses the upcoming edition's abstract deadline, falling back to the prior
        edition's -- the same date the table shows. Kept separate from the deadline
        so rows can be sorted by submission season even when their years are offset.
        """
        deadline = self.upcoming_abstract_deadline or self.prior_abstract_deadline
        return deadline.month if deadline else None

    @property
    def abstract_month_name(self) -> Optional[str]:
        """Full month name abstracts are due in (e.g. ``"April"``)."""
        month = self.abstract_month
        return calendar.month_name[month] if month else None

    @property
    def late_abstract_month(self) -> Optional[int]:
        """Month (1-12) the late abstract is due, derived from that deadline.

        Mirrors :attr:`abstract_month`: the upcoming edition's late abstract
        deadline, falling back to the prior edition's. ``None`` when the series
        has no second abstract deadline recorded.
        """
        deadline = (
            self.upcoming_late_abstract_deadline or self.prior_late_abstract_deadline
        )
        return deadline.month if deadline else None

    @property
    def late_abstract_month_name(self) -> Optional[str]:
        """Full month name late abstracts are due in (e.g. ``"October"``)."""
        month = self.late_abstract_month
        return calendar.month_name[month] if month else None

    @property
    def paper_month(self) -> Optional[int]:
        """Month (1-12) the paper is due, derived from the paper deadline.

        Mirrors :attr:`abstract_month`: the upcoming edition's paper deadline,
        falling back to the prior edition's.
        """
        deadline = self.upcoming_paper_deadline or self.prior_paper_deadline
        return deadline.month if deadline else None

    @property
    def paper_month_name(self) -> Optional[str]:
        """Full month name papers are due in (e.g. ``"May"``)."""
        month = self.paper_month
        return calendar.month_name[month] if month else None

    @property
    def location(self) -> Optional[str]:
        """Location shown in the table: upcoming, else prior."""
        return self.upcoming_location or self.prior_location

    @property
    def cost(self) -> Optional[str]:
        """Cost shown in the table: upcoming, else prior."""
        return self.upcoming_cost or self.prior_cost

    @property
    def registration(self) -> Optional[str]:
        """Registration window text shown in the table: upcoming, else prior.

        Registration is free text (windows like 'Early bird: ...; Regular: ...'),
        not a date, so there is no derived month. Mirrors the other displayed
        fields by preferring the upcoming edition's value and falling back to the
        prior edition's.
        """
        return self.upcoming_registration or self.prior_registration
