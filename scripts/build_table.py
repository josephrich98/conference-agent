"""Build (or refresh) the conference table via the discovery agent.

Discovers conferences for the requested categories, upserts them into the
database, and optionally emails a summary. Requires ``ANTHROPIC_API_KEY``.

Usage:
    python scripts/build_table.py --category radiology [--email]
"""

from __future__ import annotations

import argparse
import os

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import upsert_conferences
from conference_agent.discover import discover_conferences


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the conference table.")
    parser.add_argument(
        "--category", action="append", help="Category to search (repeatable). Default: radiology"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    parser.add_argument("--email", action="store_true", help="Email a summary when finished")
    args = parser.parse_args()

    conferences = discover_conferences(categories=args.category)
    written = upsert_conferences(conferences, db_url=args.db)
    print(f"Upserted {written} conference(s) into {args.db}")

    if args.email:
        from conference_agent.notify import notify_refresh

        sent = notify_refresh(conferences, written)
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")


if __name__ == "__main__":
    main()
