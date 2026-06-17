"""Push the stored conference table to Google Calendar.

Thin CLI wrapper over ``conference_agent.database`` +
``conference_agent.calendar_sync``. Requires ``credentials.json`` (OAuth client
secret); the first run caches a ``token.json``.

Usage:
    python scripts/sync_calendar.py [--category radiology] [--calendar-id primary]
"""

from __future__ import annotations

import argparse
import os

from conference_agent.calendar_sync import sync_conferences
from conference_agent.config import DEFAULT_DATABASE_URL, GOOGLE_CALENDAR_ID
from conference_agent.database import query_conferences


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync conferences to Google Calendar.")
    parser.add_argument("--category", help="Only sync this category")
    parser.add_argument("--calendar-id", default=GOOGLE_CALENDAR_ID, help="Target calendar id")
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    args = parser.parse_args()

    conferences = query_conferences(category=args.category, db_url=args.db)
    if not conferences:
        print("No conferences stored. Run scripts/build_table.py first.")
        return

    written = sync_conferences(conferences, calendar_id=args.calendar_id)
    print(f"Synced {written} calendar event(s) to '{args.calendar_id}'.")


if __name__ == "__main__":
    main()
