"""Build (or refresh) the conference table via the discovery agent.

Discovers conferences for the requested subcategories, upserts them into the
database, and optionally emails a summary. The default ``claude-code`` backend
runs on the local Claude Code subscription; ``--backend api`` uses the Anthropic
API and requires ``ANTHROPIC_API_KEY``.

Usage:
    python scripts/build_table.py --subcategory radiology [--backend api] [--email]
"""

from __future__ import annotations

import argparse
import os

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import known_attendance_sources, upsert_conferences
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS, discover_conferences


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the conference table.")
    parser.add_argument(
        "--subcategory",
        action="append",
        help="Subcategory (specific field) to search (repeatable). Default: radiology",
    )
    parser.add_argument(
        "--backend",
        choices=DISCOVERY_BACKENDS,
        default=DEFAULT_BACKEND,
        help="Discovery backend: 'claude-code' (default, uses your subscription) "
        "or 'api' (Anthropic API; requires ANTHROPIC_API_KEY).",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    parser.add_argument("--email", action="store_true", help="Email a summary when finished")
    args = parser.parse_args()

    hints = known_attendance_sources(db_url=args.db, subcategories=args.subcategory)
    conferences = discover_conferences(
        subcategories=args.subcategory, backend=args.backend, attendance_hints=hints
    )
    written = upsert_conferences(conferences, db_url=args.db)
    print(f"Upserted {written} conference(s) into {args.db}")

    if args.email:
        from conference_agent.notify import notify_refresh

        sent = notify_refresh(conferences, written)
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")


if __name__ == "__main__":
    main()
