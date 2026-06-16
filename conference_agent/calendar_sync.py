"""Google Calendar synchronization.

Pushes conferences and their deadlines into Google Calendar. Each conference
yields up to two events — the abstract/submission deadline and the conference
dates — each carrying a stable external id derived from `Conference.id` so that
re-syncing updates existing events instead of creating duplicates.

This is a stub: OAuth flow, event construction, and upsert are declared but not
implemented. See `conference_agent.config` for OAuth scopes and file locations.
"""

from __future__ import annotations

from typing import Iterable

from conference_agent.config import GOOGLE_CALENDAR_ID
from conference_agent.models import Conference


def get_calendar_service():
    """Authenticate (OAuth) and return a Google Calendar API service client.

    Loads `credentials.json`, caching the token in `token.json` after the first
    run. Not yet implemented.
    """
    raise NotImplementedError


def sync_conferences(
    conferences: Iterable[Conference], calendar_id: str = GOOGLE_CALENDAR_ID
) -> int:
    """Upsert calendar events for the given conferences.

    Creates/updates a deadline event and a conference-dates event per record,
    keyed on a stable external id derived from `Conference.id`. Returns the
    number of events written. Not yet implemented.
    """
    raise NotImplementedError
