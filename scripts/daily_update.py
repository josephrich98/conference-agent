"""Incremental refresh of the conference table.

Re-runs discovery for a set of subcategories, upserts the results (idempotent, so
this rolls newly announced editions into the "upcoming" columns), and emails a
summary. Intended to run on a schedule: a weekly job for flagship fields and a
monthly job for the rest (see ``.github/workflows/weekly_update.yml`` and
``monthly_update.yml``). Requires ``ANTHROPIC_API_KEY``; email requires the
SMTP_* environment variables.

The subcategory set is chosen by ``--cadence``:
  - ``due``     -> only fields holding a conference due for a per-series
    auto-check (``refresh.due_subcategories``); rows in the refreshed fields are
    stamped as checked afterward so the two-week interval is honored. This is
    the targeted "auto-check": run it often (e.g. daily) and it spends discovery
    calls only on series whose next edition is plausibly about to be announced.
  - ``weekly``  -> flagship fields (``config.weekly_subcategories()``)
  - ``monthly`` -> everything else (``config.monthly_subcategories()``)
  - ``all``     -> every seeded field (default)
``--subcategory`` overrides the cadence selection entirely.

Usage:
    python scripts/daily_update.py [--cadence due|weekly|monthly|all] [--subcategory radiology] [--no-email]
"""

from __future__ import annotations

import argparse
import os

from conference_agent.config import (
    DEFAULT_DATABASE_URL,
    STANDING_SUBCATEGORIES,
    monthly_subcategories,
    weekly_subcategories,
)
from conference_agent.database import known_attendance_sources, upsert_conferences
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS, discover_conferences
from conference_agent.notify import notify_refresh
from conference_agent.refresh import due_subcategories, mark_subcategories_checked

# Subcategories are derived from the seed list (``config``), so adding a field's
# seeds extends the refresh; ``WEEKLY_SUBCATEGORIES`` controls which run weekly.
# The ``due`` cadence is data-dependent (it inspects the stored rows), so it is
# resolved separately in ``main`` rather than from this static map.
_CADENCE_SUBCATEGORIES = {
    "weekly": weekly_subcategories,
    "monthly": monthly_subcategories,
    "all": lambda: list(STANDING_SUBCATEGORIES),
}
_CADENCE_CHOICES = sorted([*_CADENCE_SUBCATEGORIES, "due"])


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
        "--subcategory",
        action="append",
        help="Override the cadence selection with explicit subcategories (repeatable).",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    parser.add_argument(
        "--backend",
        choices=DISCOVERY_BACKENDS,
        default=DEFAULT_BACKEND,
        help="Discovery backend: 'claude-code' (default, uses your subscription) "
        "or 'api' (Anthropic API; requires ANTHROPIC_API_KEY).",
    )
    parser.add_argument("--no-email", action="store_true", help="Do not send a summary email")
    args = parser.parse_args()

    if args.subcategory:
        subcategories = args.subcategory
    elif args.cadence == "due":
        subcategories = due_subcategories(args.db)
    else:
        subcategories = _CADENCE_SUBCATEGORIES[args.cadence]()
    if not subcategories:
        if args.cadence == "due":
            print("No conferences are due for an auto-check.")
        else:
            print(f"No subcategories to refresh for cadence '{args.cadence}'.")
        return
    print(f"Refreshing {len(subcategories)} subcategor(ies) [{args.cadence}]: {', '.join(subcategories)}")
    os.makedirs("data", exist_ok=True)

    total = 0
    all_conferences = []
    for subcategory in subcategories:
        hints = known_attendance_sources(db_url=args.db, subcategories=[subcategory])
        conferences = discover_conferences(
            subcategories=[subcategory], backend=args.backend, attendance_hints=hints
        )
        written = upsert_conferences(conferences, db_url=args.db)
        all_conferences.extend(conferences)
        total += written
        print(f"{subcategory}: upserted {written} conference(s)")

    print(f"Done. Upserted {total} conference(s) into {args.db}")

    # For the auto-check cadence, record that every row in the refreshed fields
    # was just covered, so the two-week interval gates the next run. (Whether a
    # given row was "updated" is decided at selection time on the next run.)
    if args.cadence == "due" and not args.subcategory:
        stamped = mark_subcategories_checked(subcategories, db_url=args.db)
        print(f"Marked {stamped} row(s) as checked.")

    if not args.no_email:
        sent = notify_refresh(all_conferences, total)
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")


if __name__ == "__main__":
    main()
