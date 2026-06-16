"""Command-line interface for conference_agent.

Subcommands (planned):
  discover  — run the discovery agent and store results
  list      — print the stored conference table
  sync      — push stored conferences to Google Calendar

This is a stub: the argument parser is wired up but the handlers raise
NotImplementedError until the underlying modules are built.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conference-agent",
        description="Compile conferences into a table and sync with Google Calendar.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Discover conferences and store them")
    p_discover.add_argument("--topic", action="append", help="Topic to search (repeatable)")
    p_discover.add_argument("--year", type=int, help="Edition year to target")

    sub.add_parser("list", help="Print the stored conference table")

    sub.add_parser("sync", help="Push stored conferences to Google Calendar")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command in {"discover", "list", "sync"}:
        print(f"'{args.command}' is not yet implemented.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
