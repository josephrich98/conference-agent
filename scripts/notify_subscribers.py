"""Email site subscribers about conferences that changed since the last run.

Run by ``scripts/scheduled_discovery.sh`` after each refresh (and after the
redeploy, so the site already shows the new data when the email arrives). See
``conference_agent/subscriptions.py`` for the flow.

Needs ``SUBSCRIBE_SECRET`` and ``SMTP_USER`` / ``SMTP_PASSWORD``; without them it
exits without touching the snapshot, so changes are still reported once they are
configured.

Usage::

    python scripts/notify_subscribers.py [--db URL] [--state data/notify_state.json]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from conference_agent.config import DEFAULT_DATABASE_URL, SITE_URL, SMTP_USER, SUBSCRIBE_SECRET
from conference_agent.database import query_conferences
from conference_agent.notify import send_message, smtp_configured
from conference_agent.subscriptions import fetch_subscriptions, notify_subscribers


def main() -> int:
    parser = argparse.ArgumentParser(description="Email subscribers about changed conferences.")
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    parser.add_argument("--state", default="data/notify_state.json", type=Path)
    parser.add_argument("--site", default=SITE_URL, help="Live site URL (for the API and links)")
    args = parser.parse_args()

    if not (SUBSCRIBE_SECRET and smtp_configured()):
        print("Subscriber emails skipped (SUBSCRIBE_SECRET or SMTP credentials not set).")
        return 0

    report = notify_subscribers(
        query_conferences(db_url=args.db),
        state_path=args.state,
        site_url=args.site,
        secret=SUBSCRIBE_SECRET,
        from_address=SMTP_USER,
        send=send_message,
        fetch=lambda: fetch_subscriptions(args.site, SUBSCRIBE_SECRET),
    )
    for cid, changes in report.changed.items():
        print(f"  {cid}: " + "; ".join(f"{lbl} {old or '—'} -> {new or '—'}" for lbl, old, new in changes))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
