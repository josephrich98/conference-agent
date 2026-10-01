"""Email site subscribers about changed conferences and upcoming deadlines.

Run by ``scripts/scheduled_discovery.sh`` after each refresh (and after the
redeploy, so the site already shows the new data when the email arrives). See
``conference_agent/subscriptions.py`` for the flow. Two kinds of email go out:

- an update when a subscribed series' deadlines or dates changed; and
- a reminder when one of its upcoming submission deadlines is
  ``SUBSCRIBER_REMINDER_DAYS`` days away or closer (once per deadline).

Needs ``SUBSCRIBE_SECRET`` and ``SMTP_USER`` / ``SMTP_PASSWORD``; without them it
exits without touching the snapshot, so changes are still reported once they are
configured.

Usage::

    python scripts/notify_subscribers.py [--db URL] [--state data/notify_state.json]
        [--reminder-state data/reminder_state.json]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from conference_agent.config import (
    DEFAULT_DATABASE_URL,
    SITE_URL,
    SMTP_USER,
    SUBSCRIBE_SECRET,
    SUBSCRIBER_REMINDER_DAYS,
)
from conference_agent.database import query_conferences, resolve_ids
from conference_agent.notify import send_message, smtp_configured
from conference_agent.subscriptions import (
    fetch_subscriptions,
    notify_subscribers,
    send_reminders,
    subscribers_by_id,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Email subscribers about changed conferences.")
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    parser.add_argument("--state", default="data/notify_state.json", type=Path)
    parser.add_argument("--reminder-state", default="data/reminder_state.json", type=Path)
    parser.add_argument("--site", default=SITE_URL, help="Live site URL (for the API and links)")
    args = parser.parse_args()

    if not (SUBSCRIBE_SECRET and smtp_configured()):
        print("Subscriber emails skipped (SUBSCRIBE_SECRET or SMTP credentials not set).")
        return 0

    # The subscription list is fetched at most once, and only if an email is due.
    cache: dict = {}

    def fetch():
        if "subs" not in cache:
            cache["subs"] = fetch_subscriptions(args.site, SUBSCRIBE_SECRET)
        return cache["subs"]

    def resolve(ids):
        return resolve_ids(ids, db_url=args.db)

    conferences = query_conferences(db_url=args.db)
    report = notify_subscribers(
        conferences,
        state_path=args.state,
        site_url=args.site,
        secret=SUBSCRIBE_SECRET,
        from_address=SMTP_USER,
        send=send_message,
        fetch=fetch,
        resolve=resolve,
    )
    for cid, changes in report.changed.items():
        print(f"  {cid}: " + "; ".join(f"{lbl} {old or '—'} -> {new or '—'}" for lbl, old, new in changes))

    reminders = send_reminders(
        conferences,
        state_path=args.reminder_state,
        site_url=args.site,
        secret=SUBSCRIBE_SECRET,
        from_address=SMTP_USER,
        send=send_message,
        subscribers=lambda: subscribers_by_id(fetch, resolve),
        lead_days=SUBSCRIBER_REMINDER_DAYS,
    )
    return 1 if report.failed or reminders.failed else 0


if __name__ == "__main__":
    sys.exit(main())
