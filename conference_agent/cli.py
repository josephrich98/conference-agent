"""Command-line interface for conference_agent.

Subcommands:
  discover  — run the discovery agent and store results (optionally email a summary)
  seed      — populate the table from the static seed catalog (no API needed)
  add       — manually add/update conferences from flags, a CSV, or JSON (no API)
  fields    — print the field vocabulary `add` accepts (human table or --json)
  list      — print the stored conference table
  serve     — launch the web table interface (boolean search + calendar export)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

from conference_agent.config import (
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_DATABASE_URL,
)
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS
from conference_agent.models import CONFERENCE_FORMATS, RemoteOption

# --- Field registry --------------------------------------------------------
#
# One table of the fields `add` accepts, and the single source of truth for all
# three input paths: the flags, the --csv header, and the --json record keys.
# The argparse flags below are generated from it, `_build_record` maps it to
# stored field names, and `conference-agent fields` prints it -- so a new field
# is added here once and every path picks it up together.
#
# `column` is the table-facing name (a `--flag` with dashes, a CSV header, or a
# JSON key); `field` is the stored record field. The derived columns (month,
# size, category) are outputs, never inputs, so they are not listed here.


@dataclass(frozen=True)
class _Field:
    column: str  # table-facing name: --flag / CSV header / JSON key
    field: str  # stored record field
    kind: str  # "text" | "date" | "int" | "enum" | "tags" | "dates" | "identity"
    help: str


# Scalar fields: one value, mapped straight through to a stored field. Generated
# into `--flag` arguments in the order listed here.
_SCALAR_FIELDS = (
    _Field("location", "location", "text", "Host city / venue, e.g. 'Chicago, IL'"),
    _Field(
        "attendance", "attendance", "int",
        "Typical annual attendee count, e.g. 45000 (the Size column is derived "
        "from this automatically)",
    ),
    _Field(
        "attendance_year", "attendance_year", "int",
        "Year the attendance figure describes, e.g. 2025",
    ),
    _Field(
        "attendance_source", "attendance_source", "text",
        "Source URL the attendance figure was taken from (stored for provenance)",
    ),
    _Field(
        "remote_option", "remote_option", "enum",
        "Remote attendance option: " + " / ".join(o.value for o in RemoteOption),
    ),
    _Field("cost", "cost", "text", "Registration cost summary"),
    _Field("url", "url", "text", "Official conference website (the conference-name link)"),
    _Field("notes", "notes", "text", "Free-form notes"),
    _Field(
        "abstract_due", "upcoming_abstract_deadline", "date",
        "Upcoming abstract submission deadline. When an edition publishes two "
        "abstract deadlines, this is always the EARLIER, primary one",
    ),
    _Field(
        "late_abstract_due", "upcoming_late_abstract_deadline", "date",
        "Upcoming late abstract deadline: the second, later abstract deadline "
        "some series publish -- a poster-only deadline after a talk-only main "
        "one (CSHL Biological Data Science), or a late-breaking / late-poster "
        "round (ASHG, ISMB, RECOMB). Leave unset for the majority of series, "
        "which publish a single abstract deadline",
    ),
    _Field(
        "paper_due", "upcoming_paper_deadline", "date",
        "Upcoming full-paper / manuscript deadline (proceedings venues only -- "
        "leave unset for abstract-only meetings)",
    ),
    _Field(
        "registration", "upcoming_registration", "text",
        "Upcoming registration window(s), free text, e.g. 'Early bird: Jan 5 - "
        "Mar 1; Regular: Mar 2 - conference'",
    ),
    _Field(
        "deadline_time", "deadline_time", "text",
        "Time of day (with time zone) submissions close, free text, e.g. "
        "'11:59 PM ET' or '23:59 AoE'. One value when every deadline shares it; "
        "otherwise one 'kind: time' line per deadline, e.g. 'abstract: 11:59 PM "
        "ET' / 'late abstract: 5 PM ET' / 'paper: 23:59 AoE'",
    ),
    _Field(
        "prior_abstract_due", "prior_abstract_deadline", "date",
        "Prior edition's abstract submission deadline",
    ),
    _Field(
        "prior_late_abstract_due", "prior_late_abstract_deadline", "date",
        "Prior edition's late abstract deadline (see --late-abstract-due)",
    ),
    _Field(
        "prior_paper_due", "prior_paper_deadline", "date",
        "Prior edition's full-paper / manuscript deadline",
    ),
    _Field(
        "prior_registration", "prior_registration", "text",
        "Prior edition's registration window(s), free text",
    ),
)

# Fields whose value is not a plain scalar, handled explicitly in
# `_build_record`. Listed here so `conference-agent fields` documents the whole
# input vocabulary in one place.
_COMPOSITE_FIELDS = (
    _Field(
        "conference", "acronym + name", "identity",
        "The conference as the table's first column shows it: 'ACRONYM - Full "
        "Name'. A bare acronym updates an existing row. Required (per row)",
    ),
    _Field(
        "subcategory", "subcategory", "tags",
        "One or more specific-field tags, e.g. radiology 'machine learning' "
        "(comma-separated in a CSV cell). The broad Category column is derived "
        "from these automatically",
    ),
    _Field(
        "format", "format", "tags",
        "Submission/presentation format(s) offered, any of: "
        + " / ".join(CONFERENCE_FORMATS),
    ),
    _Field(
        "conference_dates", "upcoming_start_date + upcoming_end_date", "dates",
        "Upcoming conference date(s): START [END] (space-separated in a CSV cell)",
    ),
    _Field(
        "prior_conference_dates", "prior_start_date + prior_end_date", "dates",
        "Prior edition's conference date(s): START [END]",
    ),
)

# Table-facing column -> stored field, for the scalar fields. The raw stored
# field names are accepted as aliases too (each field maps to itself), so the web
# table's "Export CSV" re-imports unchanged. `size` and `category` are derived, so
# they are accepted from an export and ignored on write.
_COLUMN_TO_FIELD = {f.column: f.field for f in _SCALAR_FIELDS}
_COLUMN_TO_FIELD.update({f.field: f.field for f in _SCALAR_FIELDS})
_COLUMN_TO_FIELD.update(
    {
        "name": "name",
        "remote": "remote_option",
        "size": "size",  # accepted from a CSV export but ignored on write (derived)
        "prior_start_date": "prior_start_date",
        "prior_end_date": "prior_end_date",
        "upcoming_start_date": "upcoming_start_date",
        "upcoming_end_date": "upcoming_end_date",
    }
)


def _flag_for(column: str) -> str:
    """The `--flag` spelling of a table-facing column name."""
    return "--" + column.replace("_", "-")


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

    ``fields`` maps the table's column names -- the vocabulary defined by
    :data:`_SCALAR_FIELDS` + :data:`_COMPOSITE_FIELDS`, shared by the flags, the
    ``--csv`` header, and the ``--json`` record keys -- to their (string or list)
    values. Run ``conference-agent fields`` to print the full vocabulary.
    The raw stored field names are accepted as aliases too, so a table CSV export
    round-trips (the derived ``size``, ``category`` and ``*_month`` columns are
    accepted but ignored on write). Returns a dict keyed by stored field names,
    suitable for ``merge_records`` / ``Conference``.
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

    # conference_dates is a START [END] pair: a list (flags) or a
    # whitespace-separated cell (csv), mirroring the table's single dates column.
    # The prior edition's pair works the same way under prior_conference_dates.
    for column, start_field, end_field in (
        ("conference_dates", "upcoming_start_date", "upcoming_end_date"),
        ("prior_conference_dates", "prior_start_date", "prior_end_date"),
    ):
        dates = fields.get(column)
        if dates in (None, "", []):
            continue
        parts = dates if isinstance(dates, list) else str(dates).split()
        if len(parts) > 2:
            raise ValueError(f"{column} takes at most two dates: START [END].")
        if parts:
            record[start_field] = parts[0]
        if len(parts) == 2:
            record[end_field] = parts[1]
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
        help="Manually add or update conferences (no API): one via flags, or many "
        "via --csv / --json",
        description="Add or update conferences without the discovery agent. "
        "Three interchangeable inputs -- flags for one conference, --csv or "
        "--json for many -- all share one field vocabulary; run "
        "`conference-agent fields` to print it (or `fields --json` for a "
        "machine-readable schema). The flags mirror the web table's columns; the "
        "Category, Size and month columns are derived on write and cannot be set "
        "by hand. By default only the fields you supply are written, so an "
        "existing series keeps the rest of its data; pass --overwrite to replace "
        "the whole row (unsupplied fields are cleared).",
    )
    p_add.add_argument(
        "--csv",
        help="CSV file whose header columns are the field names printed by "
        "`conference-agent fields` (the flag names without the leading dashes). "
        "The web table's 'Export CSV' is a valid input -- the raw stored field "
        "names are accepted as aliases and the derived 'size' / 'category' / "
        "'*_month' columns are ignored on write. Each row is one conference.",
    )
    p_add.add_argument(
        "--json",
        dest="json_path",
        help="JSON file holding one record object, or a list of them, keyed by "
        "the same field names as --csv. The most convenient path for an agent: "
        "run `conference-agent fields --json` for the machine-readable schema.",
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
    p_add.add_argument(
        "--conference-dates",
        nargs="+",
        metavar="YYYY-MM-DD",
        help="Upcoming conference date(s): START [END]",
    )
    p_add.add_argument(
        "--prior-conference-dates",
        nargs="+",
        metavar="YYYY-MM-DD",
        help="Prior edition's conference date(s): START [END]",
    )
    # Every scalar field gets a flag, generated from the registry so the flag
    # surface can never fall behind the CSV/JSON vocabulary.
    for spec in _SCALAR_FIELDS:
        kwargs: dict = {"help": spec.help}
        if spec.kind == "int":
            kwargs["type"] = int
            kwargs["metavar"] = "YYYY" if spec.column.endswith("_year") else "N"
        elif spec.kind == "date":
            kwargs["metavar"] = "YYYY-MM-DD"
        elif spec.kind == "enum":
            kwargs["choices"] = [o.value for o in RemoteOption]
        else:
            kwargs["metavar"] = "TEXT"
        p_add.add_argument(_flag_for(spec.column), dest=spec.column, **kwargs)
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

    p_fields = sub.add_parser(
        "fields",
        help="Print the fields `add` accepts (the flag / CSV / JSON vocabulary)",
        description="Print every field `conference-agent add` accepts, with the "
        "flag spelling, the value it takes, and what it means. This is the "
        "authoritative input contract -- it is generated from the same registry "
        "that defines the flags, so it can never fall out of date. Pass --json "
        "for a machine-readable version (for an agent building a --json record).",
    )
    p_fields.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the field list as JSON instead of a table.",
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


def _rows_to_records(rows: list, source: str) -> list[dict]:
    """Convert raw CSV/JSON row dicts into storage records, one per conference."""
    records = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{source} entry {index} is not an object.")
        present = {k: v for k, v in row.items() if v not in (None, "", [])}
        record = _build_record(present)
        if not record.get("acronym"):
            raise ValueError(
                f"{source} entry {index} has no 'conference' (or 'acronym') value."
            )
        records.append(record)
    return records


def _load_add_records(args) -> list[dict]:
    """Collect the conference record(s) for `add` from --csv, --json, or the flags.

    All three paths share the table's column vocabulary (see :data:`_SCALAR_FIELDS`)
    and route through :func:`_build_record`, so a CSV header, a JSON key, and a
    flag with the same name behave identically.
    """
    if args.csv and args.json_path:
        raise ValueError("Pass either --csv or --json, not both.")

    if args.csv:
        import csv

        with open(args.csv, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return _rows_to_records(rows, "CSV row")

    if args.json_path:
        import json

        with open(args.json_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise ValueError("--json must hold a record object or a list of them.")
        return _rows_to_records(payload, "JSON record")

    if not args.conference:
        raise ValueError("--conference is required when not using --csv or --json.")
    # Flag values, keyed by their table-facing column name. The scalar flags are
    # generated from the registry, so reading them back from the registry keeps
    # the two ends in step automatically.
    fields = {
        "conference": args.conference,
        "subcategory": args.subcategory,
        "format": args.format,
        "conference_dates": args.conference_dates,
        "prior_conference_dates": args.prior_conference_dates,
    }
    for spec in _SCALAR_FIELDS:
        fields[spec.column] = getattr(args, spec.column)
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


# Fields the table shows but `add` never accepts: they are derived on write from
# the inputs above, so setting them by hand is impossible by design.
_DERIVED_FIELDS = (
    ("category", "Derived from subcategory (models.SUBCATEGORY_TO_CATEGORY)"),
    ("size", "Derived from attendance (models.size_for_attendance)"),
    ("abstract_month", "Derived from abstract_due (upcoming, else prior)"),
    ("late_abstract_month", "Derived from late_abstract_due (upcoming, else prior)"),
    ("paper_month", "Derived from paper_due (upcoming, else prior)"),
    ("conference_month", "Derived from conference_dates (upcoming, else prior)"),
)

# How each field kind is written on the command line / in a file.
_KIND_VALUE = {
    "text": "text",
    "date": "YYYY-MM-DD",
    "int": "integer",
    "enum": "one of: " + ", ".join(o.value for o in RemoteOption),
    "tags": "one or more tags (space-separated as a flag, comma-separated in a cell)",
    "dates": "START [END], both YYYY-MM-DD",
    "identity": "'ACRONYM - Full Name', or a bare ACRONYM to update",
}


def _cmd_fields(args) -> int:
    """Print the input vocabulary shared by --flag, --csv header, and --json key."""
    specs = list(_COMPOSITE_FIELDS) + list(_SCALAR_FIELDS)

    if args.as_json:
        import json

        payload = {
            "fields": [
                {
                    "name": f.column,
                    "flag": _flag_for(f.column),
                    "value": _KIND_VALUE[f.kind],
                    "stored_as": f.field,
                    "required": f.column == "conference",
                    "description": f.help,
                }
                for f in specs
            ],
            "derived": [{"name": n, "description": d} for n, d in _DERIVED_FIELDS],
        }
        print(json.dumps(payload, indent=2))
        return 0

    from tabulate import tabulate

    table = [[_flag_for(f.column), f.column, _KIND_VALUE[f.kind], f.help] for f in specs]
    print(
        "Fields accepted by `conference-agent add`. The same names work three "
        "ways:\n  a --flag, a --csv header column, or a --json record key.\n"
    )
    print(tabulate(table, headers=["Flag", "Name", "Value", "Meaning"], tablefmt="github"))
    print("\nDerived columns (never accepted as input -- computed on write):\n")
    print(
        tabulate(
            [[n, d] for n, d in _DERIVED_FIELDS],
            headers=["Column", "Derived from"],
            tablefmt="github",
        )
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
        "fields": _cmd_fields,
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
