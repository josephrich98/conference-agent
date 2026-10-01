"""Incremental refresh of the conference table.

Re-runs discovery for a set of subcategories, upserts the results (idempotent, so
this rolls newly announced editions into the "upcoming" columns), and emails a
summary. The default ``claude-code`` backend uses the local ``claude`` CLI; the
``api`` backend requires ``ANTHROPIC_API_KEY``. Email requires the SMTP_*
environment variables.

``--cadence watch`` is the scheduled job (``scripts/scheduled_discovery.sh``,
daily). Instead of whole fields it works per series: it sorts conferences into
tiers by how close their deadlines and meeting are, runs a cheap, agent-free
check of each one's official pages, and re-researches only the series whose
pages changed (see ``conference_agent.refresh.run_watch``). ``--dry-run`` shows
what it would do without researching or writing anything.

The other cadences choose a subcategory set:
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
    python scripts/daily_update.py [--cadence watch|due|weekly|monthly|all] [--subcategory radiology] [--no-email] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys

from conference_agent.config import (
    DEFAULT_DATABASE_URL,
    monthly_subcategories,
    weekly_subcategories,
)
from conference_agent.database import (
    discovery_subcategories,
    known_attendance_sources,
    query_conferences,
    roll_past_editions,
    upsert_conferences,
)
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS, discover_conferences
from conference_agent.notify import notify_refresh
from conference_agent.refresh import due_subcategories, mark_subcategories_checked, run_watch

# Each cadence picks from the fields in the table; ``WEEKLY_SUBCATEGORIES``
# controls which run weekly. The ``due`` cadence inspects the stored rows' ages,
# so it is resolved separately in ``main``.
_CADENCE_SUBCATEGORIES = {
    "weekly": weekly_subcategories,
    "monthly": monthly_subcategories,
    "all": list,
}
_CADENCE_CHOICES = sorted([*_CADENCE_SUBCATEGORIES, "due", "watch"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Conference table refresh.")
    parser.add_argument(
        "--cadence",
        choices=_CADENCE_CHOICES,
        default="all",
        help="What to refresh: watch (page-gated, per series; the scheduled job), "
        "due (fields holding stale series), weekly (flagship fields), monthly "
        "(the rest), or all.",
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="watch cadence only: run the page checks and report what would be "
        "researched, without calling the agent or writing to the database.",
    )
    args = parser.parse_args()

    if args.cadence == "watch" and not args.subcategory:
        sys.exit(_run_watch(args))

    if args.subcategory:
        subcategories = args.subcategory
    elif args.cadence == "due":
        subcategories = due_subcategories(args.db)
    else:
        subcategories = _CADENCE_SUBCATEGORIES[args.cadence](discovery_subcategories(args.db))
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
    known = query_conferences(db_url=args.db)
    for subcategory in subcategories:
        hints = known_attendance_sources(db_url=args.db, subcategories=[subcategory])
        conferences = discover_conferences(
            subcategories=[subcategory], backend=args.backend, attendance_hints=hints,
            known=known,
        )
        written = upsert_conferences(conferences, db_url=args.db)
        all_conferences.extend(conferences)
        total += written
        print(f"{subcategory}: upserted {written} conference(s)")

    print(f"Done. Upserted {total} conference(s) into {args.db}")
    rolled = roll_past_editions(args.db)
    print(
        f"Merged {len(rolled['duplicate'])} duplicated edition(s); "
        f"moved {len(rolled['rolled'])} finished edition(s) to prior."
    )

    # For the auto-check cadence, record that every row in the refreshed fields
    # was just covered, so the two-week interval gates the next run. (Whether a
    # given row was "updated" is decided at selection time on the next run.)
    if args.cadence == "due" and not args.subcategory:
        stamped = mark_subcategories_checked(subcategories, db_url=args.db)
        print(f"Marked {stamped} row(s) as checked.")

    if not args.no_email:
        sent = notify_refresh(all_conferences, total)
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")


def _run_watch(args) -> int:
    """The page-gated watch cycle; returns the process exit code."""
    print(f"Watch run [{'dry run' if args.dry_run else args.backend}] on {args.db}")
    report = run_watch(db_url=args.db, backend=args.backend, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    if report.refreshed and not args.no_email:
        sent = notify_refresh(report.refreshed, len(report.researched))
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")
    # A failed batch is retried next run, but surface it so the job reports failure.
    return 1 if report.failed else 0


if __name__ == "__main__":
    main()
