"""Command-line interface for conference_agent.

Subcommands:
  discover  — run the discovery agent and store results (optionally email a summary)
  seed      — populate the table from the static seed catalog (no API needed)
  list      — print the stored conference table
  serve     — launch the web table interface (boolean search + calendar export)
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from conference_agent.config import (
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_DATABASE_URL,
)
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conference-agent",
        description="Compile conferences into a table and export them as a calendar feed.",
    )
    parser.add_argument("--db", default=DEFAULT_DATABASE_URL, help="SQLAlchemy database URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Discover conferences and store them")
    p_discover.add_argument(
        "--category", action="append", help="Category to search, e.g. radiology (repeatable)"
    )
    p_discover.add_argument(
        "--backend",
        choices=DISCOVERY_BACKENDS,
        default=DEFAULT_BACKEND,
        help="Discovery backend: 'claude-code' (default) drives the local Claude "
        "Code CLI on your subscription (cheaper); 'api' uses the Anthropic API "
        "(requires ANTHROPIC_API_KEY and credits).",
    )
    p_discover.add_argument(
        "--model",
        help="Override the model id (defaults to the config model for --backend "
        "api, or Claude Code's configured model for --backend claude-code).",
    )
    p_discover.add_argument("--email", action="store_true", help="Email a summary when finished")

    p_seed = sub.add_parser(
        "seed", help="Populate the table from the static seed catalog (no API needed)"
    )
    p_seed.add_argument(
        "--overwrite",
        action="store_true",
        help="Refresh seed-derived fields on existing rows too (default: insert missing only)",
    )

    p_list = sub.add_parser("list", help="Print the stored conference table")
    p_list.add_argument("--category", help="Filter by category")
    p_list.add_argument("--reputation", help="Filter by reputation (big/medium/small)")

    p_serve = sub.add_parser("serve", help="Launch the web table interface")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    return parser


def _cmd_discover(args) -> int:
    import os

    from conference_agent.database import upsert_conferences
    from conference_agent.discover import discover_conferences

    if args.backend == "api" and not os.environ.get(ANTHROPIC_API_KEY_ENV):
        print(
            f"Error: --backend api requires {ANTHROPIC_API_KEY_ENV} to be set.",
            file=sys.stderr,
        )
        return 1

    conferences = discover_conferences(
        categories=args.category, backend=args.backend, model=args.model
    )
    written = upsert_conferences(conferences, db_url=args.db)
    print(f"Discovered and stored {written} conference(s).")

    if args.email:
        from conference_agent.notify import notify_refresh

        sent = notify_refresh(conferences, written)
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")
    return 0


def _cmd_seed(args) -> int:
    from conference_agent.database import seed_conferences

    written = seed_conferences(db_url=args.db, overwrite=args.overwrite)
    verb = "Wrote" if args.overwrite else "Inserted"
    print(f"{verb} {written} seed conference row(s).")
    return 0


def _cmd_list(args) -> int:
    from tabulate import tabulate

    from conference_agent.database import query_conferences

    rows = query_conferences(category=args.category, reputation=args.reputation, db_url=args.db)
    if not rows:
        print("No conferences stored. Run `conference-agent discover` first.")
        return 0

    table = [
        [
            c.acronym,
            c.category,
            c.reputation.value if c.reputation else "",
            c.upcoming_abstract_deadline or "",
            c.upcoming_start_date or "",
            c.remote_option.value if c.remote_option else "",
        ]
        for c in rows
    ]
    headers = ["Acronym", "Category", "Tier", "Abstract due", "Upcoming", "Remote"]
    print(tabulate(table, headers=headers, tablefmt="github"))
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("web.app:app", host=args.host, port=args.port)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "discover": _cmd_discover,
        "seed": _cmd_seed,
        "list": _cmd_list,
        "serve": _cmd_serve,
    }
    try:
        return handlers[args.command](args)
    except NotImplementedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
