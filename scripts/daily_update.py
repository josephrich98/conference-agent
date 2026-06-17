"""Incremental refresh of the conference table.

Re-runs discovery for a set of categories, upserts the results (idempotent, so
this rolls newly announced editions into the "upcoming" columns), and emails a
summary. Intended to run on a schedule: a weekly job for flagship fields and a
monthly job for the rest (see ``.github/workflows/weekly_update.yml`` and
``monthly_update.yml``). Requires ``ANTHROPIC_API_KEY``; email requires the
SMTP_* environment variables.

The category set is chosen by ``--cadence``:
  - ``due``     -> only fields holding a conference due for a per-series
    auto-check (``refresh.due_categories``); rows in the refreshed fields are
    stamped as checked afterward so the two-week interval is honored. This is
    the targeted "auto-check": run it often (e.g. daily) and it spends discovery
    calls only on series whose next edition is plausibly about to be announced.
  - ``weekly``  -> flagship fields (``config.weekly_categories()``)
  - ``monthly`` -> everything else (``config.monthly_categories()``)
  - ``all``     -> every seeded field (default)
``--category`` overrides the cadence selection entirely.

Usage:
    python scripts/daily_update.py [--cadence due|weekly|monthly|all] [--category radiology] [--no-email]
"""

from __future__ import annotations

import argparse
import os

from conference_agent.config import (
    DEFAULT_DATABASE_URL,
    STANDING_CATEGORIES,
    monthly_categories,
    weekly_categories,
)
from conference_agent.database import upsert_conferences
from conference_agent.discover import discover_conferences
from conference_agent.notify import notify_refresh
from conference_agent.refresh import due_categories, mark_categories_checked

# Categories are derived from the seed list (``config``), so adding a field's
# seeds extends the refresh; ``WEEKLY_CATEGORIES`` controls which run weekly. The
# ``due`` cadence is data-dependent (it inspects the stored rows), so it is
# resolved separately in ``main`` rather than from this static map.
_CADENCE_CATEGORIES = {
    "weekly": weekly_categories,
    "monthly": monthly_categories,
    "all": lambda: list(STANDING_CATEGORIES),
}
_CADENCE_CHOICES = sorted([*_CADENCE_CATEGORIES, "due"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Conference table refresh.")
    parser.add_argument(
        "--cadence",
        choices=_CADENCE_CHOICES,
        default="all",
        help="Which fields to refresh: due (auto-check stale series), weekly "
        "(flagship), monthly (rest), or all.",
    )
    parser.add_argument(
        "--category",
        action="append",
        help="Override the cadence selection with explicit categories (repeatable).",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    parser.add_argument("--no-email", action="store_true", help="Do not send a summary email")
    args = parser.parse_args()

    if args.category:
        categories = args.category
    elif args.cadence == "due":
        categories = due_categories(args.db)
    else:
        categories = _CADENCE_CATEGORIES[args.cadence]()
    if not categories:
        if args.cadence == "due":
            print("No conferences are due for an auto-check.")
        else:
            print(f"No categories to refresh for cadence '{args.cadence}'.")
        return
    print(f"Refreshing {len(categories)} categor(ies) [{args.cadence}]: {', '.join(categories)}")
    os.makedirs("data", exist_ok=True)

    total = 0
    all_conferences = []
    for category in categories:
        conferences = discover_conferences(categories=[category])
        written = upsert_conferences(conferences, db_url=args.db)
        all_conferences.extend(conferences)
        total += written
        print(f"{category}: upserted {written} conference(s)")

    print(f"Done. Upserted {total} conference(s) into {args.db}")

    # For the auto-check cadence, record that every row in the refreshed fields
    # was just covered, so the two-week interval gates the next run. (Whether a
    # given row was "updated" is decided at selection time on the next run.)
    if args.cadence == "due" and not args.category:
        stamped = mark_categories_checked(categories, db_url=args.db)
        print(f"Marked {stamped} row(s) as checked.")

    if not args.no_email:
        sent = notify_refresh(all_conferences, total)
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")


if __name__ == "__main__":
    main()
