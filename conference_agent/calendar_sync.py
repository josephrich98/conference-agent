"""Calendar export — an iCalendar (``.ics``) feed of a conference's deadlines.

Each conference can yield up to three all-day events for its upcoming edition:

- the upcoming abstract submission deadline
- the upcoming full paper / manuscript deadline
- the upcoming conference dates (start through end)

(Registration is a free-text field — windows, not a single date — so it yields
no calendar event.)

The feed is the credential-free path to a user's calendar: they subscribe to a
URL (Google "Add by URL", Apple/Outlook "Add from URL") or download a one-off
``.ics``. There is no OAuth and no API key, and generation is pure Python, so it
runs from the static/Lambda web layer. Each event carries a stable, deterministic
id derived from ``Conference.id`` and the event kind, so re-fetching updates the
existing event instead of creating a duplicate (idempotent).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional

from conference_agent.config import (
    CALENDAR_REMINDER_HOUR,
    CALENDAR_REMINDER_LEAD_DAYS,
)
from conference_agent.models import Conference


@dataclass(frozen=True)
class CalEvent:
    """One calendar event for a conference's upcoming edition.

    The single source of truth for *what* events a conference yields. ``start``
    and ``end`` are both **inclusive** (``end`` is the event's last day); the ICS
    renderer applies the exclusive-end convention itself.
    """

    kind: str
    summary: str
    start: date
    end: date
    description: str


def _event_id(conference_id: str, kind: str) -> str:
    """Deterministic event id for a conference + event kind.

    Base32hex-encodes a stable key so the same conference/kind always maps to the
    same id, which is what lets a re-fetched feed update an event in place rather
    than duplicate it.
    """
    key = f"{conference_id}-{kind}".encode("utf-8")
    encoded = base64.b32hexencode(key).decode("ascii").lower().rstrip("=")
    return f"conf{encoded}"


def _edition_events(conf: Conference) -> List[CalEvent]:
    """The upcoming-edition events a conference yields.

    Up to three: the abstract deadline, the paper deadline, and the conference
    dates. Only populated upcoming fields produce an event. ``start``/``end`` are
    inclusive (a single-day deadline has ``start == end``). Registration is free
    text, so it produces no event.
    """
    events: List[CalEvent] = []
    label = f"{conf.acronym} {conf.name}"
    url = f"\n{conf.url}" if conf.url else ""

    if conf.upcoming_abstract_deadline:
        events.append(
            CalEvent(
                "abstract",
                f"{conf.acronym} — abstract deadline",
                conf.upcoming_abstract_deadline,
                conf.upcoming_abstract_deadline,
                f"Abstract submission deadline for {label}.{url}",
            )
        )
    if conf.upcoming_paper_deadline:
        events.append(
            CalEvent(
                "paper",
                f"{conf.acronym} — paper deadline",
                conf.upcoming_paper_deadline,
                conf.upcoming_paper_deadline,
                f"Full paper / manuscript deadline for {label}.{url}",
            )
        )
    if conf.upcoming_start_date:
        end = conf.upcoming_end_date or conf.upcoming_start_date
        events.append(
            CalEvent(
                "conference",
                f"{conf.acronym} {conf.upcoming_start_date.year}",
                conf.upcoming_start_date,
                end,
                f"{label} conference dates.{url}",
            )
        )
    # Registration is free text (windows, not a single date), so it yields no
    # calendar event.
    return events


# --- ICS feed --------------------------------------------------------------

# Calendar-application hint for how often to re-fetch a subscribed feed.
ICS_REFRESH = "PT12H"


def _ics_escape(text: str) -> str:
    """Escape a value for an iCalendar TEXT property (RFC 5545 §3.3.11)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_fold(line: str) -> str:
    """Fold a content line to <=75 octets per RFC 5545 §3.1.

    Continuation lines begin with a single space. Folding is done on UTF-8 octets
    without splitting a multibyte character.
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    pieces: List[bytes] = []
    start, limit = 0, 75
    while len(data) - start > limit:
        end = start + limit
        # Don't split a multibyte sequence: back up over continuation bytes.
        while end > start and (data[end] & 0xC0) == 0x80:
            end -= 1
        pieces.append(data[start:end])
        start, limit = end, 74  # continuation lines lose one octet to the space
    pieces.append(data[start:])
    return "\r\n ".join(p.decode("utf-8") for p in pieces)


def _alarm_trigger(days: int) -> str:
    """RFC 5545 ``TRIGGER`` for a reminder ``days`` days before an all-day event.

    An all-day ``DTSTART`` is midnight, so a bare ``-P{days}D`` fires at 00:00 and
    several calendar clients then bucket it as one day *earlier* than intended (a
    midnight alarm precedes their "N days before (9 AM)" preset, so "1 day before"
    shows up as "2 days before"). Anchoring the alarm at
    :data:`CALENDAR_REMINDER_HOUR` on the target morning makes the "N days before"
    label display correctly. Example (``days=1``, hour 9): fire at 09:00 the day
    before → ``-PT15H``.
    """
    hours_before = days * 24 - CALENDAR_REMINDER_HOUR
    whole_days, rem_hours = divmod(hours_before, 24)
    if whole_days and rem_hours:
        return f"-P{whole_days}DT{rem_hours}H"
    if whole_days:
        return f"-P{whole_days}D"
    return f"-PT{rem_hours}H"


def _ics_lines_for(conf: Conference, dtstamp: str) -> List[str]:
    """VEVENT blocks (as unfolded lines) for one conference's upcoming edition."""
    lines: List[str] = []
    for ev in _edition_events(conf):
        uid = f"{_event_id(conf.id, ev.kind)}@conference-agent"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{ev.start.strftime('%Y%m%d')}",
            # An all-day DTEND is exclusive: last day + 1.
            f"DTEND;VALUE=DATE:{(ev.end + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(ev.summary)}",
            f"DESCRIPTION:{_ics_escape(ev.description)}",
        ]
        if conf.url:
            lines.append(f"URL:{conf.url}")
        lines.append("TRANSP:TRANSPARENT")
        # One reminder per configured lead time (see CALENDAR_REMINDER_LEAD_DAYS),
        # each anchored to the morning so the "N days before" label is exact.
        for days in sorted(set(CALENDAR_REMINDER_LEAD_DAYS), reverse=True):
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_ics_escape(ev.summary)}",
                f"TRIGGER:{_alarm_trigger(days)}",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    return lines


def conferences_to_ics(
    conferences: Iterable[Conference],
    calendar_name: str = "Conference Agent",
    dtstamp: Optional[datetime] = None,
) -> str:
    """Render conferences as an iCalendar (``.ics``) document.

    Produces up to three all-day events per conference (abstract deadline, paper
    deadline, conference dates) for the upcoming edition, each with reminders at
    the configured lead times (default 28 days, 7 days, and 1 day ahead).
    ``dtstamp`` defaults to the current UTC time; pass a fixed value for
    deterministic output. Lines use CRLF and are folded per RFC 5545, so the
    result is suitable to serve as ``text/calendar``.
    """
    stamp = (dtstamp or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Conference Agent//Conference Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"NAME:{_ics_escape(calendar_name)}",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
        f"REFRESH-INTERVAL;VALUE=DURATION:{ICS_REFRESH}",
        f"X-PUBLISHED-TTL:{ICS_REFRESH}",
    ]
    for conf in conferences:
        lines += _ics_lines_for(conf, stamp)
    lines.append("END:VCALENDAR")
    return "".join(_ics_fold(line) + "\r\n" for line in lines)
