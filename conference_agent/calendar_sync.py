"""Google Calendar synchronization.

Pushes each conference's upcoming edition into Google Calendar. A conference can
yield up to three events:

- the upcoming abstract submission deadline
- the upcoming full paper / manuscript deadline
- the upcoming conference dates (start through end)

Each event carries a stable, deterministic event id derived from
``Conference.id`` and the event kind, so re-syncing updates the existing event
instead of creating a duplicate (idempotent upsert).

OAuth, event construction, and upsert live here. See ``conference_agent.config``
for OAuth scopes and file locations. The Google client libraries are an optional
dependency (``pip install -e ".[calendar]"``) and are imported lazily.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from conference_agent.config import (
    CALENDAR_REMINDER_LEAD_DAYS,
    GOOGLE_CALENDAR_ID,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_MAX_REMINDER_MINUTES,
    GOOGLE_OAUTH_SCOPES,
    GOOGLE_TOKEN_FILE,
)
from conference_agent.models import Conference


@dataclass(frozen=True)
class CalEvent:
    """One calendar event for a conference's upcoming edition.

    The single source of truth for *what* events a conference yields, shared by
    the Google Calendar sync (:func:`_events_for`) and the ICS feed
    (:func:`conferences_to_ics`) so the two targets never drift. ``start`` and
    ``end`` are both **inclusive** (``end`` is the event's last day); each target
    applies its own exclusive-end convention when rendering.
    """

    kind: str
    summary: str
    start: date
    end: date
    description: str


def _reminders() -> dict:
    """Reminder overrides attached to every synced event.

    One popup reminder per lead time in :data:`CALENDAR_REMINDER_LEAD_DAYS`
    (one month, one week, one day ahead). Google caps a reminder override at
    :data:`GOOGLE_MAX_REMINDER_MINUTES` (4 weeks), so the one-month lead is
    clamped to that maximum; duplicate offsets after clamping are dropped.
    """
    minutes = sorted(
        {
            min(days * 24 * 60, GOOGLE_MAX_REMINDER_MINUTES)
            for days in CALENDAR_REMINDER_LEAD_DAYS
        }
    )
    return {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": m} for m in minutes],
    }


def _event_id(conference_id: str, kind: str) -> str:
    """Deterministic Google Calendar event id for a conference + event kind.

    Google requires event ids to use base32hex characters (``0-9a-v``) and be
    5–1024 chars. We base32hex-encode a stable key so the same conference/kind
    always maps to the same id, which is what makes re-syncing idempotent.
    """
    key = f"{conference_id}-{kind}".encode("utf-8")
    encoded = base64.b32hexencode(key).decode("ascii").lower().rstrip("=")
    return f"conf{encoded}"


def get_calendar_service():
    """Authenticate (OAuth) and return a Google Calendar API service client.

    Loads ``credentials.json`` (OAuth client secret), caching the token in
    ``token.json`` after the first run. Refreshes an expired token silently.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_OAUTH_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_FILE, GOOGLE_OAUTH_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _all_day_event(
    event_id: str, summary: str, start: date, end: date, description: str
) -> dict:
    """Build an all-day event body. Google's end date is exclusive.

    Each event carries reminders one month, one week, and one day ahead (see
    :func:`_reminders`).
    """
    return {
        "id": event_id,
        "summary": summary,
        "description": description,
        "start": {"date": start.isoformat()},
        "end": {"date": (end + timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "reminders": _reminders(),
    }


def _edition_events(conf: Conference) -> List[CalEvent]:
    """The upcoming-edition events a conference yields (target-independent).

    Up to three: the abstract deadline, the paper deadline, and the conference
    dates. Only populated upcoming fields produce an event. ``start``/``end`` are
    inclusive (a single-day deadline has ``start == end``).
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
    return events


def _events_for(conf: Conference) -> List[dict]:
    """Construct the upcoming-edition Google Calendar event bodies."""
    return [
        _all_day_event(
            _event_id(conf.id, ev.kind), ev.summary, ev.start, ev.end, ev.description
        )
        for ev in _edition_events(conf)
    ]


def _upsert_event(service, calendar_id: str, event: dict) -> None:
    """Insert the event, or update it if its id already exists."""
    from googleapiclient.errors import HttpError

    try:
        service.events().insert(calendarId=calendar_id, body=event).execute()
    except HttpError as exc:
        if exc.resp.status == 409:  # already exists → update in place
            service.events().update(
                calendarId=calendar_id, eventId=event["id"], body=event
            ).execute()
        else:
            raise


def sync_conferences(
    conferences: Iterable[Conference],
    calendar_id: str = GOOGLE_CALENDAR_ID,
    service=None,
) -> int:
    """Upsert calendar events for the given conferences.

    Creates/updates up to three events per record (abstract deadline, paper
    deadline, conference dates) for the upcoming edition, keyed on a stable
    event id derived from ``Conference.id``. Returns the number of events
    written. Pass ``service`` to reuse an authenticated client; otherwise one is
    created via :func:`get_calendar_service`.
    """
    if service is None:
        service = get_calendar_service()

    written = 0
    for conf in conferences:
        for event in _events_for(conf):
            _upsert_event(service, calendar_id, event)
            written += 1
    return written


# --- ICS feed --------------------------------------------------------------
#
# An iCalendar (RFC 5545) feed is the credential-free path to a user's calendar:
# they subscribe to a URL (Google "Add by URL", Apple/Outlook "Add from URL") or
# download a one-off ``.ics``. No OAuth, no ``credentials.json``, and it works
# from the static/Lambda web layer because generation is pure Python. Each event
# reuses the same stable id as the Google sync (see :func:`_event_id`) so the
# feed is idempotent: re-fetching updates events in place rather than duplicating.

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
            # All-day DTEND is exclusive, like Google's: last day + 1.
            f"DTEND;VALUE=DATE:{(ev.end + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(ev.summary)}",
            f"DESCRIPTION:{_ics_escape(ev.description)}",
        ]
        if conf.url:
            lines.append(f"URL:{conf.url}")
        lines.append("TRANSP:TRANSPARENT")
        # One reminder per configured lead time (one month, one week, one day).
        # ICS triggers are relative to DTSTART and carry no 4-week cap, so the
        # full 30-day lead survives here (Google clamps it; see _reminders).
        for days in sorted(set(CALENDAR_REMINDER_LEAD_DAYS), reverse=True):
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_ics_escape(ev.summary)}",
                f"TRIGGER:-P{days}D",
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
    deadline, conference dates) for the upcoming edition, each with reminders one
    month, one week, and one day ahead. ``dtstamp`` defaults to the current UTC
    time; pass a fixed value for deterministic output. Lines use CRLF and are
    folded per RFC 5545, so the result is suitable to serve as ``text/calendar``.
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


def conference_event_ids(conf: Conference) -> List[Tuple[str, str]]:
    """Return the (kind, event_id) pairs a conference would sync.

    Useful for building a per-row "sync" / deep link in the table interface.
    """
    kinds = []
    if conf.upcoming_abstract_deadline:
        kinds.append("abstract")
    if conf.upcoming_paper_deadline:
        kinds.append("paper")
    if conf.upcoming_start_date:
        kinds.append("conference")
    return [(k, _event_id(conf.id, k)) for k in kinds]
