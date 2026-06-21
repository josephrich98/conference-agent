"""Command-line interface for conference_agent.

Subcommands:
  discover  — run the discovery agent and store results (optionally email a summary)
  seed      — populate the table from the static seed catalog (no API needed)
  add       — manually add/update conferences from flags or a CSV (no API needed)
  list      — print the stored conference table
  serve     — launch the web table interface (boolean search + calendar export)
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Optional, Sequence

from conference_agent.config import (
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_DATABASE_URL,
)
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS
from conference_agent.models import CONFERENCE_FORMATS, RemoteOption

# The `add` flags and the --csv header share one vocabulary: the web table's
# column names (the only extra is url, the link behind the conference name). Each
# entry maps a table-facing column to the stored record field. The month columns
# are derived from the dates by the database, so they are not inputs; conference
# (acronym + name), subcategory (multi-valued), and conference_dates (start/end
# pair) are handled separately in _build_record. Raw stored field names appear on
# the right as their own keys too, so a table "Export CSV" re-imports unchanged.
_COLUMN_TO_FIELD = {
    "location": "location",
    "size": "size",  # accepted from a CSV export but ignored on write (derived)
    "attendance": "attendance",
    "attendance_year": "attendance_year",
    "attendance_source": "attendance_source",
    "remote": "remote_option",
    "remote_option": "remote_option",
    "cost": "cost",
    "url": "url",
    "notes": "notes",
    "name": "name",
    "abstract_due": "upcoming_abstract_deadline",
    "paper_due": "upcoming_paper_deadline",
    "registration": "upcoming_registration",
    "upcoming_abstract_deadline": "upcoming_abstract_deadline",
    "upcoming_paper_deadline": "upcoming_paper_deadline",
    "upcoming_start_date": "upcoming_start_date",
    "upcoming_end_date": "upcoming_end_date",
    "upcoming_registration": "upcoming_registration",
    "prior_abstract_deadline": "prior_abstract_deadline",
    "prior_paper_deadline": "prior_paper_deadline",
    "prior_start_date": "prior_start_date",
    "prior_end_date": "prior_end_date",
    "prior_registration": "prior_registration",
}

# The table's "Conference" column reads "ACRONYM — Name"; --conference (and the
# csv "conference" column) accept the same form. Split on the first spaced
# dash/colon so hyphenated names (e.g. "Computer-Assisted") survive: the left side
# is the acronym (the row key), the right side the full name. A bare value (no
# separator) is just the acronym, which is enough to update an existing row.
_CONFERENCE_SPLIT = re.compile(r"\s+[-—–:]\s+")


def _parse_conference(value: str) -> tuple[str, "str | None"]:
    parts = _CONFERENCE_SPLIT.split(value.strip(), maxsplit=1)
    acronym = parts[0].strip()
    name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return acronym, name


def _build_record(fields: dict) -> dict:
    """Build a storage record from table-facing column/flag values.

    ``fields`` maps the table's column names -- the vocabulary shared by the flags
    and the ``--csv`` header (conference, subcategory, location, attendance,
    attendance_year, attendance_source, remote/remote_option, cost, abstract_due,
    paper_due, conference_dates, registration, url, notes) -- to their (string or
    list) values.
    The raw stored field names are accepted as aliases too, so a table CSV export
    round-trips (the derived ``size`` and ``category`` columns are accepted but
    ignored on write). Returns a dict keyed by stored field names, suitable for
    ``merge_records`` / ``Conference``.
    """
    record: dict = {}
    # Identity: the "conference" column is "ACRONYM - Name"; explicit acronym / id
    # columns (from a raw export) take precedence when present.
    if fields.get("conference"):
        acronym, name = _parse_conference(str(fields["conference"]))
        if acronym:
            record["acronym"] = acronym
        if name:
            record["name"] = name
    for key in ("acronym", "id"):
        if fields.get(key):
            record["acronym"] = str(fields[key]).strip()

    for column, field in _COLUMN_TO_FIELD.items():
        value = fields.get(column)
        if value not in (None, ""):
            record[field] = value

    # Subcategory (the granular tag column): a list (flags) or a delimited cell
    # (csv). Accepts "subcategory"/"subcategories", and the legacy "category"
    # column as an alias so older exports still ingest. The broad "category" column
    # of a current export is derived, so it is ignored here (subcategory wins).
    subcategory = fields.get("subcategory")
    if subcategory in (None, "", []):
        subcategory = fields.get("subcategories")
    if subcategory in (None, "", []):
        subcategory = fields.get("category")  # legacy export column
    if subcategory not in (None, "", []):
        record["subcategory"] = subcategory

    # Formats (abstract/paper/poster/oral): a list (flags) or a delimited cell
    # (csv). Accepts the singular "format" or plural "formats" column; normalized
    # downstream. Mirrors subcategory -- multi-valued, handled separately here.
    formats = fields.get("format")
    if formats in (None, "", []):
        formats = fields.get("formats")
    if formats not in (None, "", []):
        record["format"] = formats

    # conference_dates is the upcoming START [END] pair: a list (flags) or a
    # whitespace-separated cell (csv), mirroring the table's single dates column.
    dates = fields.get("conference_dates")
    if dates not in (None, "", []):
        parts = dates if isinstance(dates, list) else str(dates).split()
        if len(parts) > 2:
            raise ValueError("conference_dates takes at most two dates: START [END].")
        if parts:
            record["upcoming_start_date"] = parts[0]
        if len(parts) == 2:
            record["upcoming_end_date"] = parts[1]
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conference-agent",
        description="Compile conferences into a table and export them as a calendar feed.",
    )
    parser.add_argument("--db", default=DEFAULT_DATABASE_URL, help="SQLAlchemy database URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Discover conferences and store them")
    p_discover.add_argument(
        "--subcategory",
        action="append",
        help="Subcategory (specific field) to search, e.g. radiology (repeatable)",
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

    p_add = sub.add_parser(
        "add",
        help="Manually add or update conferences (no API): one via flags, or many via --csv",
        description="Add or update conferences without the discovery agent. The "
        "flags mirror the web table's columns (plus --url, the link behind the "
        "conference name); the submission/conference month columns are derived "
        "from the dates automatically. By default only the fields you supply are "
        "written, so an existing series keeps the rest of its data; pass "
        "--overwrite to replace the whole row (unsupplied fields are cleared).",
    )
    p_add.add_argument(
        "--csv",
        help="CSV file whose header columns are field names (acronym or id, name, "
        "subcategory, location, attendance, attendance_year, attendance_source, "
        "remote_option, cost, url, the upcoming_*/prior_* date columns, notes). The "
        "web table's 'Export CSV' is a valid input (the derived 'size' and "
        "'category' columns are ignored on write). Each row is one conference.",
    )
    p_add.add_argument(
        "--conference",
        metavar="'ACRONYM - Name'",
        help="The conference, as it appears in the table's first column: "
        "'ACRONYM - Full Name' (e.g. 'RSNA - Radiological Society of North "
        "America Annual Meeting'). A bare acronym updates an existing row. "
        "Required unless --csv.",
    )
    p_add.add_argument(
        "--subcategory",
        nargs="+",
        metavar="TAG",
        help="One or more subcategory (specific-field) tags, space-separated, e.g. "
        "--subcategory radiology 'machine learning'. The broad Category column is "
        "derived from these automatically.",
    )
    p_add.add_argument(
        "--format",
        nargs="+",
        choices=list(CONFERENCE_FORMATS),
        metavar="FORMAT",
        help="One or more submission/presentation formats the conference offers, "
        "space-separated: any of abstract, paper, poster, oral "
        "(e.g. --format abstract poster oral)",
    )
    p_add.add_argument("--location", help="Host city / venue")
    p_add.add_argument(
        "--attendance",
        type=int,
        metavar="N",
        help="Typical annual attendee count, e.g. 45000 (the Size column is derived "
        "from this automatically).",
    )
    p_add.add_argument(
        "--attendance-year",
        type=int,
        metavar="YYYY",
        help="Year the attendance figure describes, e.g. 2025",
    )
    p_add.add_argument(
        "--attendance-source",
        help="Source URL the attendance figure was taken from (stored for provenance)",
    )
    p_add.add_argument(
        "--remote-option",
        choices=[o.value for o in RemoteOption],
        help="Remote attendance option: in-person / virtual / hybrid / unknown",
    )
    p_add.add_argument("--cost", help="Registration cost summary")
    p_add.add_argument(
        "--abstract-due", metavar="YYYY-MM-DD", help="Upcoming abstract submission deadline"
    )
    p_add.add_argument(
        "--paper-due", metavar="YYYY-MM-DD", help="Upcoming full-paper / manuscript deadline"
    )
    p_add.add_argument(
        "--conference-dates",
        nargs="+",
        metavar="YYYY-MM-DD",
        help="Upcoming conference date(s): START [END]",
    )
    p_add.add_argument(
        "--registration",
        metavar="TEXT",
        help="Upcoming registration info, free text (e.g. 'Early bird: Jan 5 - Mar 1; Regular: Mar 2 - conference')",
    )
    p_add.add_argument("--url", help="Official conference website (the conference-name link)")
    p_add.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the entire row instead of merging: fields you do not supply "
        "are cleared (requires acronym, name, and a subcategory per conference).",
    )
    p_add.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt shown when a conference matches an "
        "existing table entry (assume yes and update it).",
    )

    p_list = sub.add_parser("list", help="Print the stored conference table")
    p_list.add_argument("--category", help="Filter by broad category (e.g. medicine)")
    p_list.add_argument("--subcategory", help="Filter by subcategory (e.g. radiology)")
    p_list.add_argument("--size", help="Filter by size (large/medium/small)")

    p_serve = sub.add_parser("serve", help="Launch the web table interface")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    return parser


def _cmd_discover(args) -> int:
    import os

    from conference_agent.database import known_attendance_sources, upsert_conferences
    from conference_agent.discover import discover_conferences

    if args.backend == "api" and not os.environ.get(ANTHROPIC_API_KEY_ENV):
        print(
            f"Error: --backend api requires {ANTHROPIC_API_KEY_ENV} to be set.",
            file=sys.stderr,
        )
        return 1

    # Feed prior attendance sources back in so a refresh re-checks them first.
    hints = known_attendance_sources(db_url=args.db, subcategories=args.subcategory)
    conferences = discover_conferences(
        subcategories=args.subcategory, backend=args.backend, model=args.model,
        attendance_hints=hints,
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


def _load_add_records(args) -> list[dict]:
    """Collect the conference record(s) for `add` from --csv or the flags.

    Both paths use the table's column vocabulary and route through
    :func:`_build_record`, so a CSV column behaves exactly like its flag.
    """
    if args.csv:
        import csv

        with open(args.csv, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        records = []
        for index, row in enumerate(rows, start=1):
            present = {k: v for k, v in row.items() if v not in (None, "")}
            record = _build_record(present)
            if not record.get("acronym"):
                raise ValueError(f"CSV row {index} has no 'conference' (or 'acronym') value.")
            records.append(record)
        return records

    if not args.conference:
        raise ValueError("--conference is required when not using --csv.")
    fields = {
        "conference": args.conference,
        "subcategory": args.subcategory,
        "format": args.format,
        "location": args.location,
        "attendance": args.attendance,
        "attendance_year": args.attendance_year,
        "attendance_source": args.attendance_source,
        "remote_option": args.remote_option,
        "cost": args.cost,
        "abstract_due": args.abstract_due,
        "paper_due": args.paper_due,
        "conference_dates": args.conference_dates,
        "registration": args.registration,
        "url": args.url,
    }
    record = _build_record({k: v for k, v in fields.items() if v not in (None, "")})
    if not record.get("acronym"):
        raise ValueError("--conference must include an acronym.")
    return [record]


def _warn_new_subcategories(records: list[dict], db_url: str) -> None:
    """Warn (without failing) when a record introduces an unfamiliar subcategory tag.

    Subcategory is the one free-form categorical column, so a typo would silently
    create a new tag. Compare each tag against the known vocabulary -- the seed
    taxonomy plus tags already in the table -- and flag any newcomer on stderr.
    """
    from conference_agent.config import seed_subcategories
    from conference_agent.database import _record_subcategory, distinct_subcategories
    from conference_agent.models import normalize_subcategories

    known = set(seed_subcategories()) | distinct_subcategories(db_url)
    flagged: list[str] = []
    for record in records:
        for tag in normalize_subcategories(_record_subcategory(record)):
            if tag not in known and tag not in flagged:
                flagged.append(tag)
    for tag in flagged:
        print(
            f"Warning: '{tag}' is a new subcategory not used by any existing "
            "conference; adding it anyway.",
            file=sys.stderr,
        )


def _confirm_existing_matches(records: list[dict], db_url: str, assume_yes: bool) -> list[dict]:
    """Confirm before updating records that match an existing table entry.

    Identity keys on the (upper-cased) acronym, so both a bare acronym and the
    'ACRONYM - Name' form resolve to the same row: either is a match. For each
    record whose id already exists, prompt the curator to confirm they mean to
    update that entry; ``--yes`` skips every prompt. Declined records are dropped
    from the returned list so the rest still proceed. A non-interactive stdin
    (no TTY) is treated as a decline -- run with --yes to update unattended.
    """
    if assume_yes:
        return records

    from conference_agent.database import query_conferences

    existing = {c.id: c for c in query_conferences(db_url=db_url)}
    proceed: list[dict] = []
    for record in records:
        acronym = (record.get("acronym") or record.get("id") or "").strip()
        match = existing.get(acronym.upper()) if acronym else None
        if match is None:
            proceed.append(record)
            continue
        # Show the existing entry as the table does: "ACRONYM — Name", collapsing
        # to just the name when the row has no distinct acronym (acronym == name).
        if match.name and match.acronym.strip().lower() == match.name.strip().lower():
            label = match.name
        else:
            label = f"{match.acronym} — {match.name}" if match.name else match.acronym
        try:
            reply = input(
                f"'{label}' already exists in the table. Update this existing entry? [y/N] "
            )
        except EOFError:
            reply = ""
        if reply.strip().lower() in ("y", "yes"):
            proceed.append(record)
        else:
            print(
                f"Skipped {match.id} (existing entry left unchanged).",
                file=sys.stderr,
            )
    return proceed


def _cmd_add(args) -> int:
    from conference_agent.database import merge_records, upsert_conferences

    try:
        records = _load_add_records(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not records:
        print("No conference records to add.", file=sys.stderr)
        return 1

    _warn_new_subcategories(records, args.db)

    records = _confirm_existing_matches(records, args.db, args.yes)
    if not records:
        print("No conferences added (existing entries left unchanged).")
        return 0

    if args.overwrite:
        from pydantic import ValidationError

        from conference_agent.models import Conference

        conferences = []
        for record in records:
            data = dict(record)
            # merge_records accepts `id` as an alias for acronym; mirror that here.
            data.setdefault("acronym", data.pop("id", None))
            try:
                conferences.append(Conference(**data))
            except ValidationError as exc:
                ident = data.get("acronym") or "<unknown>"
                print(f"Error: cannot build conference {ident}: {exc}", file=sys.stderr)
                return 1
        written = upsert_conferences(conferences, db_url=args.db)
        print(f"Overwrote {written} conference row(s).")
        return 0

    written = merge_records(records, db_url=args.db)
    print(f"Added/updated {written} conference(s).")
    if written < len(records):
        print(
            "Note: new conferences require at least name and a subcategory; "
            "existing rows update only the fields you supply.",
            file=sys.stderr,
        )
    return 0


def _cmd_list(args) -> int:
    from tabulate import tabulate

    from conference_agent.database import query_conferences

    rows = query_conferences(
        subcategory=args.subcategory, category=args.category, size=args.size, db_url=args.db
    )
    if not rows:
        print("No conferences stored. Run `conference-agent discover` first.")
        return 0

    table = [
        [
            c.acronym,
            c.category,
            c.subcategory,
            c.size.value if c.size else "",
            c.attendance_display or "",
            c.upcoming_abstract_deadline or "",
            c.upcoming_start_date or "",
            c.conference_month_name or "",
            c.remote_option.value if c.remote_option else "",
        ]
        for c in rows
    ]
    headers = ["Acronym", "Category", "Subcategory", "Size", "Attendance", "Abstract due", "Upcoming", "Conf. month", "Remote"]
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
        "add": _cmd_add,
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
