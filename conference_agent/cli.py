"""Command-line interface for conference_agent.

Subcommands:
  discover  — run the discovery agent and store results (optionally email a summary)
  add       — manually add conferences from flags, a CSV, or JSON (no API);
              `add --update` changes existing ones, `add --fields` prints the
              field vocabulary it accepts
  delete    — delete a conference manually
  lookup    — print the unique values of columns, optionally over a search
              (the website's boolean query, falling back to its keyword match)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from conference_agent.config import (
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_DATABASE_URL,
)
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS
from conference_agent.models import (
    CATEGORIES,
    CONFERENCE_FORMATS,
    ConferenceSize,
    RemoteOption,
    normalize_categories,
    normalize_subcategories,
)
from web.search import RESULT_COLUMNS

# --- Field registry --------------------------------------------------------
#
# Entries are indexed by `conference_name`: `add` refuses a name that already
# exists, and `add --update` / `delete` find their row by it. Names compare by
# their id (`models.name_id`, a slug), so case, accents, punctuation, and spacing
# do not distinguish two names. Two series may share an acronym.
#
# One table of the fields `add` accepts, and the single source of truth for all
# three input paths: the flags, the --csv header, and the --json record keys.
# The argparse flags below are generated from it, `_build_record` maps it to
# stored field names, and `conference-agent add --fields` prints it -- so a new field
# is added here once and every path picks it up together.
#
# `column` is the table-facing name (a `--flag` with dashes, a CSV header, or a
# JSON key); `field` is the stored record field. The derived columns (month,
# size, category) are outputs, never inputs, so they are not listed here.


@dataclass(frozen=True)
class _Field:
    column: str  # table-facing name: --flag / CSV header / JSON key
    field: str  # stored record field
    kind: str  # "text" | "date" | "int" | "enum" | "bool" | "tags" | "dates" | "extensions"
    help: str


# Scalar fields: one value, mapped straight through to a stored field. Generated
# into `--flag` arguments in the order listed here.
_SCALAR_FIELDS = (
    _Field(
        "conference_acronym", "acronym", "text",
        "Short name, e.g. 'RSNA' (the table links it to the url). Omit when the "
        "series has no acronym. Several series may share one",
    ),
    _Field(
        "conference_name", "name", "text",
        "Full conference name, e.g. 'Radiological Society of North America "
        "Annual Meeting'. Required: entries are indexed by it, so `add` fails "
        "if it already exists and --update / `delete` fail if it does not",
    ),
    _Field(
        "new_conference_name", "new_name", "text",
        "With --update: rename the conference. Its id, page URL, and calendar "
        "event ids follow the new name; the old ones keep resolving",
    ),
    _Field(
        "location", "upcoming_location", "text",
        "Upcoming edition's host city / venue, e.g. 'Chicago, IL'",
    ),
    _Field(
        "stable_location", "stable_location", "bool",
        "Whether the conference is held in the same place every edition (e.g. "
        "RSNA in Chicago): true or false. A prior edition's location is then "
        "shown as current",
    ),
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
    _Field("cost", "upcoming_cost", "text", "Upcoming edition's registration cost summary"),
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
        "abstract_time", "abstract_time", "text",
        "Time of day the abstract deadline closes. 24-hour ('23:59') or 12-hour "
        "('11:59 PM'); stored as 24-hour HH:MM",
    ),
    _Field(
        "abstract_timezone", "abstract_timezone", "text",
        "Zone of the abstract deadline time: AoE, UTC, a region code (ET, CT, MT, "
        "PT, CET, ...; EST/EDT and 'Eastern Time' are accepted), UTC+N, or an IANA "
        "name. The region code covers daylight time",
    ),
    _Field(
        "late_abstract_time", "late_abstract_time", "text",
        "Time of day the late abstract deadline closes (see --abstract-time)",
    ),
    _Field(
        "late_abstract_timezone", "late_abstract_timezone", "text",
        "Zone of the late abstract deadline time (see --abstract-timezone)",
    ),
    _Field(
        "paper_time", "paper_time", "text",
        "Time of day the paper deadline closes (see --abstract-time)",
    ),
    _Field(
        "paper_timezone", "paper_timezone", "text",
        "Zone of the paper deadline time (see --abstract-timezone)",
    ),
    _Field(
        "deadline_time", "deadline_time", "text",
        "Shorthand for the six *_time / *_timezone fields: free text such as "
        "'11:59 PM ET' (applied to every deadline the conference has) or one "
        "'abstract: 5 PM ET' / 'paper: 23:59 AoE' entry per line. Parsed into the "
        "structured fields; any explicit *_time / *_timezone value wins",
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
    _Field(
        "prior_location", "prior_location", "text",
        "Prior edition's host city / venue",
    ),
    _Field(
        "prior_cost", "prior_cost", "text",
        "Prior edition's registration cost summary",
    ),
)

# Fields whose value is not a plain scalar, handled explicitly in
# `_build_record`. Listed here so `conference-agent add --fields` documents the whole
# input vocabulary in one place.
_COMPOSITE_FIELDS = (
    _Field(
        "subcategory", "subcategory", "tags",
        "One or more specific-field tags, e.g. radiology 'machine learning' "
        "(comma-separated in a CSV cell)",
    ),
    _Field(
        "category", "category", "tags",
        "One or more broad categories, usually from: " + " / ".join(CATEGORIES)
        + " (any other value is kept as entered; comma-separated in a CSV cell)",
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
    _Field(
        "deadline_extensions",
        "abstract_deadline_extension + late_abstract_deadline_extension "
        "+ paper_deadline_extension",
        "extensions",
        "Deadline extension history: one entry per extension, each a type "
        "(abstract / late abstract / paper), the original date, and the extended "
        "date. Added to the stored history; entries whose extended date is more "
        "than 5 years old are not kept",
    ),
)

# Table-facing column -> stored field, for the scalar fields. The raw stored
# field names are accepted as aliases too (each field maps to itself), so the web
# table's "Export CSV" re-imports unchanged. `size` is derived, so it is accepted
# from an export and ignored on write.
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


# Legacy shorthand: older CSV/JSON files (and the hidden --conference flag) carry
# one "conference" value in the table's "ACRONYM — Name" display form. Split on the first spaced
# dash/colon so hyphenated names (e.g. "Computer-Assisted") survive: the left side
# is the acronym (the row key), the right side the full name. A bare value (no
# separator) is just the acronym.
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
    values. Run ``conference-agent add --fields`` to print the full vocabulary.
    The raw stored field names are accepted as aliases too, so a table CSV export
    round-trips (the derived ``size``, ``category`` and ``*_month`` columns are
    accepted but ignored on write). Returns a dict keyed by stored field names,
    suitable for ``merge_records`` / ``Conference``.
    """
    record: dict = {}
    # Identity: conference_acronym / conference_name (or the raw acronym / name
    # columns of an export) are the fields; the legacy combined "conference"
    # value is parsed first so explicit fields win over it.
    if fields.get("conference"):
        acronym, name = _parse_conference(str(fields["conference"]))
        if acronym:
            record["acronym"] = acronym
        if name:
            record["name"] = name

    for column, field in _COLUMN_TO_FIELD.items():
        value = fields.get(column)
        if value not in (None, ""):
            record[field] = value.strip() if field in ("acronym", "name") else value

    if not record.get("acronym") and fields.get("id"):
        record["acronym"] = str(fields["id"]).strip()

    # Subcategory and category (the granular and broad tag columns): a list
    # (flags) or a delimited cell (csv), singular or plural column name.
    for key in ("subcategory", "category"):
        value = fields.get(key)
        if value in (None, "", []):
            value = fields.get(key.replace("y", "ies"))
        if value not in (None, "", []):
            record[key] = value

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

    # Extension history: objects (json), TYPE ORIGINAL EXTENDED triples (the
    # repeated flag), or ";"-separated entries (csv). Parsed here so a malformed
    # entry is reported before anything is written.
    extensions = fields.get("deadline_extensions")
    if extensions not in (None, "", []):
        from conference_agent.database import parse_deadline_extensions

        record["deadline_extensions"] = parse_deadline_extensions(extensions)
    return record


# --- lookup ----------------------------------------------------------------
#
# `lookup` lists the unique values of the table's columns (the API / snapshot
# columns) across a search. Multi-valued tag columns are split so each tag is
# one value; `increasing` ranks size by magnitude rather than by name.
# ``location`` / ``cost`` are the displayed values (upcoming, else prior), each
# placed just before its upcoming column so the order matches the site's table.
_LOOKUP_DISPLAYED = ("location", "cost")
_LOOKUP_COLUMNS = tuple(
    c
    for col in RESULT_COLUMNS
    for c in (
        (col.removeprefix("upcoming_"), col)
        if col.removeprefix("upcoming_") in _LOOKUP_DISPLAYED
        else (col,)
    )
)
_LOOKUP_TAG_COLUMNS = {"category", "subcategory", "format"}
_LOOKUP_SORTS = ("alphabetical", "reversealphabetical", "increasing", "decreasing")
_SIZE_ORDER = {s.value: i for i, s in enumerate(reversed(list(ConferenceSize)))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conference-agent",
        description="Compile conferences into a table and export them as a calendar feed.",
    )
    parser.add_argument("--db", default=DEFAULT_DATABASE_URL, help="SQLAlchemy database URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser(
        "discover",
        help="Discover conferences and store them",
        description="Search for conferences and store them. With no filter, every "
        "subcategory in the table is surveyed (one agent run per field). "
        "--subcategory / --category limit the survey to those fields. "
        "--conference-name / --size instead re-check matching stored series only "
        "(no new series are added); any --subcategory / --category also narrows "
        "that selection. Run `discover --options` for the valid values.",
    )
    p_discover.add_argument(
        "--options",
        action="store_true",
        help="List the valid values for each filter flag and exit",
    )
    p_discover.add_argument(
        "--subcategory",
        action="append",
        help="Subcategory (specific field) to search, e.g. radiology (repeatable)",
    )
    p_discover.add_argument(
        "--category",
        action="append",
        metavar="CATEGORY",
        help="Broad category to search; expands to the subcategories of the "
        "stored series in it (repeatable)",
    )
    p_discover.add_argument(
        "--conference-name",
        action="append",
        metavar="TEXT",
        help="Re-check this stored series, by its full name (repeatable)",
    )
    p_discover.add_argument(
        "--size",
        action="append",
        choices=[s.value for s in ConferenceSize],
        metavar="SIZE",
        help="Re-check stored series of this size (repeatable)",
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

    p_add = sub.add_parser(
        "add",
        help="Manually add conferences (no API): one via flags, or many via "
        "--csv / --json; --update changes existing ones",
        description="Add or update conferences without the discovery "
        "agent. Entries are indexed by --conference-name: `add` fails if the name "
        "already exists, and --update fails if it does not. "
        "Three interchangeable inputs -- flags for one conference, --csv or "
        "--json for many -- all share one field vocabulary; run "
        "`conference-agent add --fields` to print it (or `--fields json` for a "
        "machine-readable schema). The flags mirror the web table's columns; the "
        "Category, Size and month columns are derived on write and cannot be set "
        "by hand. --update writes only the fields you supply, so an existing "
        "series keeps the rest of its data; add --overwrite to replace the whole "
        "row (unsupplied fields are cleared).",
    )
    p_add.add_argument(
        "--csv",
        help="CSV file whose header columns are the field names printed by "
        "`conference-agent add --fields` (the flag names without the leading dashes). "
        "The web table's 'Export CSV' is a valid input -- the raw stored field "
        "names are accepted as aliases and the derived 'size' / 'category' / "
        "'*_month' columns are ignored on write. Each row is one conference.",
    )
    p_add.add_argument(
        "--json",
        dest="json_path",
        help="JSON file holding one record object, or a list of them, keyed by "
        "the same field names as --csv. The most convenient path for an agent: "
        "run `conference-agent add --fields json` for the machine-readable schema.",
    )
    # Legacy combined "ACRONYM - Name" shorthand, superseded by
    # --conference-acronym / --conference-name; kept so older scripts still run.
    p_add.add_argument("--conference", help=argparse.SUPPRESS)
    p_add.add_argument(
        "--subcategory",
        nargs="+",
        metavar="TAG",
        help="One or more subcategory (specific-field) tags, space-separated, e.g. "
        "--subcategory radiology 'machine learning'.",
    )
    p_add.add_argument(
        "--category",
        nargs="+",
        metavar="TAG",
        help="One or more broad categories, space-separated, usually from: "
        + ", ".join(CATEGORIES) + " (any other value is kept as entered).",
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
    p_add.add_argument(
        "--deadline-extensions",
        nargs=3,
        action="append",
        metavar=("TYPE", "ORIGINAL", "EXTENDED"),
        help="One deadline extension: TYPE (abstract, late_abstract, or paper), "
        "then the original and extended dates (YYYY-MM-DD). Repeat the flag for "
        "more entries.",
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
        elif spec.kind == "bool":
            kwargs["choices"] = ["true", "false"]
            kwargs["type"] = str.lower
        else:
            kwargs["metavar"] = "TEXT"
        p_add.add_argument(_flag_for(spec.column), dest=spec.column, **kwargs)
    p_add.add_argument(
        "--update",
        action="store_true",
        help="Update existing conferences, matched by --conference-name, instead "
        "of adding new ones (fails if a name does not exist). Only the fields "
        "you supply are written.",
    )
    p_add.add_argument(
        "--overwrite",
        action="store_true",
        help="With --update: replace the entire row instead of merging, so fields "
        "you do not supply are cleared.",
    )
    p_add.add_argument(
        "--pin",
        nargs="+",
        metavar="FIELD",
        help="Lock these fields (add's field names, e.g. url subcategory format) "
        "at their stored values so discovery never changes them. Manual edits "
        "still apply. Pins any value the same command writes.",
    )
    p_add.add_argument(
        "--unpin",
        nargs="+",
        metavar="FIELD",
        help="Release pinned fields so discovery may update them again.",
    )
    p_add.add_argument(
        "--fields",
        nargs="?",
        const="table",
        choices=["table", "json"],
        metavar="json",
        help="Print every field `add` accepts -- flag spelling, value, and "
        "meaning -- and exit without writing anything. Generated from the same "
        "registry that defines the flags, so it can never fall out of date. "
        "`--fields json` emits a machine-readable version (for an agent "
        "building a --json record).",
    )

    p_delete = sub.add_parser(
        "delete",
        help="Delete a conference manually",
        description="Delete a conference, matched by --conference-name (fails if "
        "the name does not exist). A deleted seed series returns on the next "
        "static build, and discovery may find any series again.",
    )
    p_delete.add_argument(
        "--conference-name",
        required=True,
        metavar="TEXT",
        help="Full name of the conference to delete, as the table shows it",
    )
    p_delete.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt."
    )

    p_lookup = sub.add_parser(
        "lookup",
        help="Print the unique values of one or more columns, optionally over a search",
        description="Print the unique values of each column across the conferences "
        "matching --query (all conferences when omitted). The query uses the website's "
        "search: the exact boolean query first, then, when it matches nothing or does "
        "not parse, the forgiving keyword match. Like the website, retired series are "
        "left out unless --include-retired is given.",
    )
    p_lookup.add_argument(
        "--columns",
        nargs="+",
        metavar="COLUMN",
        help="Columns to list (default: every table column). Choices: "
        + ", ".join(_LOOKUP_COLUMNS),
    )
    p_lookup.add_argument(
        "--query",
        default="",
        help='Search query, e.g. "subcategory:radiology AND size:massive" or '
        '"pediatric radiology"',
    )
    p_lookup.add_argument(
        "--sort",
        choices=_LOOKUP_SORTS,
        default="alphabetical",
        help="alphabetical (default) / reversealphabetical compare values as text; "
        "increasing / decreasing compare them by value (numbers numerically, dates "
        "chronologically, size small -> massive)",
    )
    p_lookup.add_argument(
        "--include-retired",
        action="store_true",
        help="Also search retired series (no new edition in the check window)",
    )

    return parser


def _print_discover_options(db_url: str) -> None:
    """Print the valid values for each `discover` filter flag."""
    from conference_agent.database import discovery_subcategories, distinct_categories

    def block(flag: str, values) -> None:
        print(f"{flag}:")
        for value in values:
            print(f"  {value}")
        print()

    print("--conference-name:")
    print("  any stored series' full name; list them with `conference-agent lookup --columns name`")
    print()
    block("--category", normalize_categories([*CATEGORIES, *distinct_categories(db_url)]))
    block(
        "--subcategory (fields in the table and the seed list; any other field also works)",
        discovery_subcategories(db_url),
    )
    block("--size", [s.value for s in ConferenceSize])


def _discover_survey_fields(args) -> list[str]:
    """The fields a survey run searches: the given ones, else every field."""
    from conference_agent.database import discovery_subcategories, query_conferences

    if not (args.subcategory or args.category):
        return discovery_subcategories(args.db)
    fields: list[str] = []
    for sub in normalize_subcategories(args.subcategory or []):
        if sub not in fields:
            fields.append(sub)
    if args.category:
        # A category's fields are the subcategories of the stored series in it.
        wanted = set(normalize_categories(args.category))
        for conf in query_conferences(db_url=args.db):
            if wanted & set(conf.categories):
                for sub in conf.subcategories:
                    if sub not in fields:
                        fields.append(sub)
    return fields


def _discover_targets(args, errors: list[str]) -> list:
    """Stored series a re-check run covers: every filter given must match."""
    from conference_agent.database import query_conferences
    from conference_agent.models import name_id

    rows = query_conferences(db_url=args.db)
    if args.conference_name:
        by_id = {conf.id: conf for conf in rows}
        named = []
        for name in args.conference_name:
            conf = by_id.get(name_id(name))
            if conf is None:
                errors.append(
                    f"no stored conference named '{name}' "
                    "(see `conference-agent lookup --columns name`)."
                )
            elif conf not in named:
                named.append(conf)
        rows = named
    if args.size:
        rows = [c for c in rows if c.size and c.size.value in args.size]
    if args.subcategory:
        wanted = set(normalize_subcategories(args.subcategory))
        rows = [c for c in rows if wanted & set(c.subcategories)]
    if args.category:
        wanted = set(normalize_categories(args.category))
        rows = [c for c in rows if wanted & set(c.categories)]
    return rows


def _cmd_discover(args) -> int:
    import os

    from conference_agent.database import (
        apply_refreshed_conferences,
        attendance_hints_for,
        known_attendance_sources,
        query_conferences,
        upsert_conferences,
    )
    from conference_agent.discover import discover_conferences, refresh_conferences

    if args.options:
        _print_discover_options(args.db)
        return 0

    if args.backend == "api" and not os.environ.get(ANTHROPIC_API_KEY_ENV):
        print(
            f"Error: --backend api requires {ANTHROPIC_API_KEY_ENV} to be set.",
            file=sys.stderr,
        )
        return 1

    conferences: list = []
    written = 0
    if args.conference_name or args.size:
        # Re-check mode: only stored series matching every filter, in batches.
        from conference_agent.config import WATCH_BATCH_SIZE

        errors: list[str] = []
        targets = _discover_targets(args, errors)
        if errors:
            for message in errors:
                print(f"Error: {message}", file=sys.stderr)
            return 1
        if not targets:
            print("No stored conferences match those filters.")
            return 0
        print(f"Re-checking {len(targets)} stored conference(s).")
        hints = known_attendance_sources(db_url=args.db)
        for start in range(0, len(targets), WATCH_BATCH_SIZE):
            batch = targets[start:start + WATCH_BATCH_SIZE]
            print("Researching: " + ", ".join(_label(c) for c in batch))
            found = refresh_conferences(
                batch, backend=args.backend, model=args.model,
                attendance_hints=attendance_hints_for(batch, hints),
            )
            written += apply_refreshed_conferences(found, db_url=args.db)
            conferences.extend(found)
        print(f"Re-checked and updated {written} conference(s).")
    else:
        # Survey mode: one agent run per field, stored as each finishes.
        fields = _discover_survey_fields(args)
        if not fields:
            print("No subcategories match those filters.")
            return 0
        print(f"Surveying {len(fields)} subcategor{'y' if len(fields) == 1 else 'ies'}.")
        known = query_conferences(db_url=args.db)
        for field in fields:
            # Feed prior attendance sources back in so a refresh re-checks them first.
            hints = known_attendance_sources(db_url=args.db, subcategories=[field])
            found = discover_conferences(
                subcategories=[field], backend=args.backend, model=args.model,
                attendance_hints=hints, known=known,
            )
            count = upsert_conferences(found, db_url=args.db)
            print(f"{field}: stored {count} conference(s)")
            written += count
            conferences.extend(found)
        print(f"Discovered and stored {written} conference(s).")

    if args.email:
        from conference_agent.notify import notify_refresh

        sent = notify_refresh(conferences, written)
        print("Summary email sent." if sent else "Email skipped (SMTP not configured).")
    return 0


def _rows_to_records(rows: list, source: str) -> list[dict]:
    """Convert raw CSV/JSON row dicts into storage records, one per conference."""
    records = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{source} entry {index} is not an object.")
        present = {k: v for k, v in row.items() if v not in (None, "", [])}
        record = _build_record(present)
        if not record.get("name"):
            raise ValueError(f"{source} entry {index} has no 'conference_name' value.")
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

    if not (args.conference or args.conference_name):
        raise ValueError("--conference-name is required when not using --csv or --json.")
    # Flag values, keyed by their table-facing column name. The scalar flags are
    # generated from the registry, so reading them back from the registry keeps
    # the two ends in step automatically.
    fields = {
        "conference": args.conference,  # legacy shorthand
        "subcategory": args.subcategory,
        "category": args.category,
        "format": args.format,
        "conference_dates": args.conference_dates,
        "prior_conference_dates": args.prior_conference_dates,
        "deadline_extensions": args.deadline_extensions,
    }
    for spec in _SCALAR_FIELDS:
        fields[spec.column] = getattr(args, spec.column)
    return [_build_record({k: v for k, v in fields.items() if v not in (None, "")})]


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


def _label(conf) -> str:
    """A conference as the table shows it: "ACRONYM — Name", or just the name."""
    if not conf.acronym or conf.acronym.strip().lower() == conf.name.strip().lower():
        return conf.name
    return f"{conf.acronym} — {conf.name}"


def _match_names(records: list[dict], db_url: str) -> list:
    """Each record's existing conference (matched by name id), or ``None``."""
    from conference_agent.database import query_conferences
    from conference_agent.models import name_id

    by_id = {conf.id: conf for conf in query_conferences(db_url=db_url)}
    return [by_id.get(name_id(record["name"])) for record in records]


def _require_names(records: list[dict], errors: list[str]) -> None:
    """Every record needs a conference name, the field entries are indexed by."""
    from conference_agent.models import name_id

    seen: set[str] = set()
    for index, record in enumerate(records, start=1):
        name = " ".join(str(record.get("name") or "").split())
        if not name_id(name):
            errors.append(
                f"conference {index} has no conference_name with a letter or digit "
                "(entries are indexed by it)."
            )
            continue
        record["name"] = name
        if name_id(name) in seen:
            errors.append(f"'{name}' appears more than once in the input.")
        seen.add(name_id(name))


_URL_FIELDS = ("url", "attendance_source")
_DATE_RANGES = (
    ("conference_dates", "upcoming_start_date", "upcoming_end_date"),
    ("prior_conference_dates", "prior_start_date", "prior_end_date"),
)
_MIN_ATTENDANCE_YEAR = 1900


def _check_values(records: list[dict], matches: list, errors: list[str]) -> None:
    """Reject malformed URLs, reversed date ranges, and out-of-range numbers.

    ``matches`` holds each record's stored conference (or ``None``), so an update
    that supplies only one end of a date range is checked against the other end
    already on file.
    """
    from urllib.parse import urlparse

    from conference_agent.database import (
        EXTENSION_RETENTION_YEARS,
        _coerce_date,
        extension_cutoff,
    )

    for record, match in zip(records, matches):
        name = record["name"]
        for field in _URL_FIELDS:
            value = str(record.get(field) or "").strip()
            if not value:
                continue
            # A bare domain ("rsna.org/meeting") is fine: the table adds https://.
            has_scheme = "://" in value
            parsed = urlparse(value if has_scheme else "https://" + value)
            host = parsed.hostname or ""
            if (
                parsed.scheme not in ("http", "https")
                or "." not in host.strip(".")
                or any(c.isspace() for c in value)
            ):
                errors.append(
                    f"'{name}': {field} '{value}' is not an http(s) URL "
                    "(e.g. https://www.rsna.org or rsna.org)."
                )

        for column, start_field, end_field in _DATE_RANGES:
            try:
                start = _coerce_date(record.get(start_field))
                end = _coerce_date(record.get(end_field))
            except ValueError:
                continue  # not a date at all; reported when the record is written
            if match is not None:
                start = start or getattr(match, start_field)
                end = end or getattr(match, end_field)
            if start and end and end < start:
                errors.append(f"'{name}': {column} ends ({end}) before it starts ({start}).")

        for kind, original, extended in record.get("deadline_extensions") or []:
            if extended < extension_cutoff():
                errors.append(
                    f"'{name}': the {kind.replace('_', ' ')} extension to {extended} is "
                    f"more than {EXTENSION_RETENTION_YEARS} years old (not kept)."
                )

        for field, low, high in (
            ("attendance", 1, None),
            ("attendance_year", _MIN_ATTENDANCE_YEAR, date.today().year),
        ):
            value = record.get(field)
            if value in (None, ""):
                continue
            try:
                number = int(str(value).strip())
            except ValueError:
                errors.append(f"'{name}': {field} '{value}' is not a whole number.")
                continue
            if number < low or (high is not None and number > high):
                bounds = f"between {low} and {high}" if high is not None else f"at least {low}"
                errors.append(f"'{name}': {field} {number} must be {bounds}.")


def _report(errors: list[str]) -> int:
    for message in errors:
        print(f"Error: {message}", file=sys.stderr)
    print("Nothing was written.", file=sys.stderr)
    return 1


def _cmd_add(args) -> int:
    if args.fields:
        return _print_fields(as_json=args.fields == "json")
    if args.overwrite and not args.update:
        print("Error: --overwrite applies only with --update.", file=sys.stderr)
        return 1

    try:
        records = _load_add_records(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not records:
        print("No conference records to add.", file=sys.stderr)
        return 1

    errors: list[str] = []
    _require_names(records, errors)
    pin = _resolve_pins(args.pin, errors)
    unpin = _resolve_pins(args.unpin, errors)
    if set(pin) & set(unpin):
        errors.append("a field cannot be both pinned and unpinned.")
    if errors:
        return _report(errors)
    code = _update_existing(records, args) if args.update else _add_new(records, args)
    if code == 0 and (pin or unpin):
        _apply_pins(records, pin, unpin, args.db)
    return code


def _apply_pins(records: list[dict], pin: list[str], unpin: list[str], db_url: str) -> None:
    """Pin / unpin the written records' fields (by their current, possibly new, name)."""
    from conference_agent.database import set_pins
    from conference_agent.models import name_id

    for record in records:
        name = " ".join(str(record.get("new_name") or "").split()) or record["name"]
        pinned = set_pins(name_id(name), pin=pin, unpin=unpin, db_url=db_url)
        print(f"Pinned for '{name}': {', '.join(pinned) or 'none'}.")


def _add_new(records: list[dict], args) -> int:
    """Insert conferences whose names do not exist yet; any conflict aborts all."""
    from conference_agent.database import merge_records, release_ids
    from conference_agent.models import name_id

    errors: list[str] = []
    matches = _match_names(records, args.db)
    _check_values(records, [None] * len(records), errors)
    for record, match in zip(records, matches):
        name = record["name"]
        if match is not None:
            errors.append(f"'{name}' already exists; use `add --update` to change it.")
        if record.get("new_name"):
            errors.append(f"'{name}': --new-conference-name applies only with --update.")
    if errors:
        return _report(errors)

    for record in records:
        # A series without an acronym is stored with acronym == name, which the
        # table renders as the name alone.
        record["acronym"] = (record.get("acronym") or record["name"]).strip()
        record["id"] = name_id(record["name"])
    # A name a renamed series once had is free again: drop its alias so the new
    # series is not routed to the renamed one.
    release_ids([r["id"] for r in records], db_url=args.db)
    _warn_new_subcategories(records, args.db)
    written = merge_records(records, db_url=args.db)
    print(f"Added {written} conference(s).")
    return 0


def _update_existing(records: list[dict], args) -> int:
    """Update conferences matched by name; an unknown name aborts all."""
    from conference_agent.database import (
        merge_records,
        rename_conference,
        set_acronym,
        upsert_conferences,
    )
    from conference_agent.models import name_id

    errors: list[str] = []
    matches = _match_names(records, args.db)
    taken = {name_id(r["name"]) for r in records}
    for record, match in zip(records, matches):
        if match is None:
            errors.append(
                f"'{record['name']}' does not exist; run `add` without --update to add it."
            )
        new_name = " ".join(str(record.get("new_name") or "").split())
        if new_name:
            new_id = name_id(new_name)
            if not new_id:
                errors.append(f"--new-conference-name '{new_name}' has no letter or digit.")
            elif match is not None and new_id != match.id and (
                new_id in taken or _match_names([{"name": new_name}], args.db)[0] is not None
            ):
                errors.append(f"cannot rename to '{new_name}': that name is already in use.")
            taken.add(new_id)
    # With --overwrite the stored row is replaced, so only the record's own dates count.
    _check_values(records, [None] * len(records) if args.overwrite else matches, errors)
    if errors:
        return _report(errors)

    _warn_new_subcategories(records, args.db)
    for record, match in zip(records, matches):
        record.pop("id", None)
        # The stored spelling of the name is kept (renaming is --new-conference-name).
        record["name"] = match.name
        acronym = (record.get("acronym") or "").strip()
        if acronym and acronym != match.acronym:
            set_acronym(match.id, acronym, db_url=args.db)
        record["acronym"] = acronym or match.acronym

    if args.overwrite:
        from pydantic import ValidationError

        from conference_agent.models import Conference

        conferences = []
        for record in records:
            data = {k: v for k, v in record.items() if k != "new_name"}
            try:
                conferences.append(Conference(**data))
            except ValidationError as exc:
                print(f"Error: cannot build conference {record['name']}: {exc}", file=sys.stderr)
                return 1
        written = upsert_conferences(conferences, db_url=args.db, manual=True)
        # The model carries no extension history; add any entered entries.
        merge_records(
            [
                {"id": match.id, "deadline_extensions": record["deadline_extensions"]}
                for record, match in zip(records, matches)
                if record.get("deadline_extensions")
            ],
            db_url=args.db,
        )
        print(f"Overwrote {written} conference row(s).")
    else:
        for record, match in zip(records, matches):
            record["id"] = match.id
        written = merge_records(records, db_url=args.db)
        print(f"Updated {written} conference(s).")

    for record, match in zip(records, matches):
        new_name = " ".join(str(record.get("new_name") or "").split())
        if new_name and new_name != match.name:
            new_id = rename_conference(match.id, new_name, db_url=args.db)
            print(f"Renamed '{match.name}' to '{new_name}' (id {new_id}).")
    return 0


def _delete_named(names: list[str], db_url: str, assume_yes: bool) -> int:
    """Delete conferences by name after confirmation; an unknown name aborts all."""
    from conference_agent.database import delete_conferences

    matches = _match_names([{"name": n} for n in names], db_url)
    errors = [f"'{name}' does not exist." for name, m in zip(names, matches) if m is None]
    if errors:
        return _report(errors)

    if not assume_yes:
        for match in matches:
            print(f"  {_label(match)}")
        try:
            reply = input(f"Delete {len(matches)} conference(s)? [y/N] ")
        except EOFError:
            reply = ""
        if reply.strip().lower() not in ("y", "yes"):
            print("Nothing deleted (pass --yes to skip this prompt).", file=sys.stderr)
            return 1
    removed = delete_conferences([m.id for m in matches], db_url=db_url)
    print(f"Deleted {removed} conference(s).")
    return 0


def _cmd_delete(args) -> int:
    name = args.conference_name.strip()
    if not name:
        print("Error: --conference-name must be non-empty.", file=sys.stderr)
        return 1
    return _delete_named([name], args.db, args.yes)


# Fields the table shows but `add` never accepts: they are derived on write from
# the inputs above, so setting them by hand is impossible by design.
_DERIVED_FIELDS = (
    ("size", "Derived from attendance (models.size_for_attendance)"),
    ("abstract_month", "Derived from abstract_due (upcoming, else prior)"),
    ("late_abstract_month", "Derived from late_abstract_due (upcoming, else prior)"),
    ("paper_month", "Derived from paper_due (upcoming, else prior)"),
    ("conference_month", "Derived from conference_dates (upcoming, else prior)"),
)

# Input fields with no web-table column of their own, and where (if anywhere)
# their value surfaces on the site. Every other input field is its own column.
_NOT_DISPLAYED = {
    "conference_acronym": "integrated into Conference",
    "conference_name": "integrated into Conference",
    "new_conference_name": "renames the Conference entry",
    "url": "integrated into Conference (as its link)",
    "attendance_year": "integrated into Attendance, e.g. 45,000 (2025)",
    "attendance_source": "stored for provenance only",
    "notes": "stored only",
    "abstract_time": "integrated into Deadline time",
    "abstract_timezone": "integrated into Deadline time",
    "late_abstract_time": "integrated into Deadline time",
    "late_abstract_timezone": "integrated into Deadline time",
    "paper_time": "integrated into Deadline time",
    "paper_timezone": "integrated into Deadline time",
    "prior_abstract_due": "integrated into Abstract due (shown when no upcoming date)",
    "prior_late_abstract_due": (
        "integrated into Late abstract due (shown when no upcoming date)"
    ),
    "prior_paper_due": "integrated into Paper due (shown when no upcoming date)",
    "prior_registration": "integrated into Registration (shown when no upcoming value)",
    "prior_location": "integrated into Location (shown in orange when no upcoming value)",
    "prior_cost": "integrated into Cost (shown in orange when no upcoming value)",
    "stable_location": "integrated into Location (a prior location is not marked as past)",
    "prior_conference_dates": (
        "integrated into Conference dates (shown when no upcoming dates)"
    ),
    "deadline_extensions": (
        "a ⏩ after an extended deadline date, with the past extensions on hover"
    ),
}

# How each field kind is written on the command line / in a file.
_KIND_VALUE = {
    "text": "text",
    "date": "YYYY-MM-DD",
    "int": "integer",
    "enum": "one of: " + ", ".join(o.value for o in RemoteOption),
    "bool": "true or false",
    "tags": "one or more tags (space-separated as a flag, comma-separated in a cell)",
    "dates": "START [END], both YYYY-MM-DD",
    "extensions": (
        "TYPE ORIGINAL EXTENDED per entry (a repeated flag; ';'-separated in a "
        "cell; {type, original, extended} objects in JSON)"
    ),
}


def pin_targets() -> dict[str, list[str]]:
    """`add` field name -> the stored columns pinning it locks.

    The stored column names are accepted too. Identity fields are not pinnable:
    discovery never renames a series.
    """
    from conference_agent.database import PINNABLE_FIELDS

    targets: dict[str, list[str]] = {f: [f] for f in PINNABLE_FIELDS}
    for spec in _field_specs():
        columns = [c.strip() for c in spec.field.split("+")]
        if spec.column == "deadline_time":
            columns = [f for f in PINNABLE_FIELDS if f.endswith(("_time", "_timezone"))]
        if all(c in PINNABLE_FIELDS for c in columns):
            targets[spec.column] = columns
    return targets


def _resolve_pins(names, errors: list[str]) -> list[str]:
    """Stored columns for the `--pin` / `--unpin` names (unknown ones are errors)."""
    targets = pin_targets()
    out: list[str] = []
    for name in names or []:
        columns = targets.get(name.strip().lower().replace("-", "_"))
        if columns is None:
            errors.append(f"cannot pin '{name}'; pinnable fields: {', '.join(sorted(targets))}.")
        else:
            out.extend(c for c in columns if c not in out)
    return out


def _field_specs() -> list[_Field]:
    """The `add` input fields in display order (identity, composites, the rest)."""
    return list(_SCALAR_FIELDS[:2]) + list(_COMPOSITE_FIELDS) + list(_SCALAR_FIELDS[2:])


def add_field_schema() -> dict:
    """The `add` input vocabulary as JSON-friendly data.

    Printed by ``conference-agent add --fields json`` and exported by
    ``scripts/build_static.py`` for the website's "Add a conference" form, whose
    submissions are ``--json`` records keyed by these names.
    """
    from conference_agent.deadline_time import TIMEZONES

    return {
        "fields": [
            {
                "name": f.column,
                "flag": _flag_for(f.column),
                "kind": f.kind,
                "value": _KIND_VALUE[f.kind],
                "stored_as": f.field,
                # One of the two identity fields is required per row.
                "required": f.column in ("conference_acronym", "conference_name"),
                "description": f.help,
                "website": _NOT_DISPLAYED.get(f.column, "own column"),
            }
            for f in _field_specs()
        ],
        "derived": [{"name": n, "description": d} for n, d in _DERIVED_FIELDS],
        "remote_options": [o.value for o in RemoteOption],
        "formats": list(CONFERENCE_FORMATS),
        "timezones": list(TIMEZONES),
        # The suggested categories, offered in the form's Category dropdown.
        "categories": list(CATEGORIES),
    }


def _print_fields(as_json: bool) -> int:
    """Print the input vocabulary shared by --flag, --csv header, and --json key."""
    specs = _field_specs()

    if as_json:
        import json

        print(json.dumps(add_field_schema(), indent=2))
        return 0

    import shutil
    import textwrap

    width = min(shutil.get_terminal_size((100, 24)).columns, 100)
    bold, dim, reset = ("\033[1m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("",) * 3

    def entry(name: str, description: str, note: str = "") -> None:
        print(f"  {bold}{name}{reset}")
        for line in textwrap.wrap(description, width - 6):
            print(f"      {line}")
        if note:
            print(f"      {dim}Not displayed on website — {note}{reset}")
        print()

    print(
        "Fields accepted by `conference-agent add`. Each name works as a --flag "
        "(underscores\nbecome dashes), a --csv header column, or a --json record key.\n"
    )
    for f in specs:
        entry(f.column, f.help, _NOT_DISPLAYED.get(f.column, ""))
    print("Derived columns (never accepted as input; computed on write):\n")
    for name, description in _DERIVED_FIELDS:
        entry(name, description)
    return 0


def _lookup_values(rows: list[dict], column: str, sort: str) -> list:
    """The unique non-empty values of ``column`` across ``rows``, sorted."""
    values = set()
    for row in rows:
        value = row.get(column)
        if value is None or value == "":
            continue
        if column in _LOOKUP_TAG_COLUMNS:
            values.update(t.strip() for t in str(value).split(",") if t.strip())
        else:
            values.add(value)
    if sort in ("alphabetical", "reversealphabetical"):
        key = lambda v: (str(v).casefold(), str(v))  # noqa: E731
    elif column == "size":
        key = lambda v: (_SIZE_ORDER.get(v, len(_SIZE_ORDER)), str(v))  # noqa: E731
    else:
        key = lambda v: (  # noqa: E731
            (0, v, "") if isinstance(v, (int, float)) else (1, 0, str(v).casefold())
        )
    return sorted(values, key=key, reverse=sort in ("reversealphabetical", "decreasing"))


def _cmd_lookup(args) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow, get_engine
    from conference_agent.refresh import is_retired
    from web.search import QueryError, build_filter, keyword_search

    columns = list(dict.fromkeys(args.columns or _LOOKUP_COLUMNS))
    unknown = [c for c in columns if c not in _LOOKUP_COLUMNS]
    if unknown:
        print(
            f"Error: unknown column(s): {', '.join(unknown)}. "
            f"Valid columns: {', '.join(_LOOKUP_COLUMNS)}",
            file=sys.stderr,
        )
        return 1

    def to_dicts(stmt) -> list[dict]:
        today = date.today()
        with Session(get_engine(args.db)) as session:
            return [
                {col: getattr(r, col) for col in RESULT_COLUMNS}
                | {
                    col: getattr(r, f"upcoming_{col}") or getattr(r, f"prior_{col}")
                    for col in _LOOKUP_DISPLAYED
                }
                for r in session.scalars(stmt)
                if args.include_retired or not is_retired(r, today)
            ]

    # Same flow as the website's search box: the exact boolean query first, and
    # the keyword ranking when it matches nothing or does not parse.
    query = args.query or ""
    query_error = None
    rows: list[dict] = []
    try:
        clause = build_filter(query)
        stmt = select(ConferenceRow)
        rows = to_dicts(stmt if clause is None else stmt.where(clause))
    except QueryError as exc:
        query_error = str(exc)
    if not rows and query.strip():
        rows = keyword_search(query, to_dicts(select(ConferenceRow)))
        if rows:
            why = f"Not a valid boolean query ({query_error})" if query_error else "No exact matches"
            print(f"{why}; using the keyword match.", file=sys.stderr)
    if not rows:
        print(f"Query error: {query_error}" if query_error else "No matching conferences.", file=sys.stderr)
        return 1 if query_error else 0

    for i, column in enumerate(columns):
        values = _lookup_values(rows, column, args.sort)
        if len(columns) > 1:
            if i:
                print()
            print(f"{column}:")
        for value in values:
            text = value.isoformat() if hasattr(value, "isoformat") else str(value)
            print(f"  {text}" if len(columns) > 1 else text)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "discover": _cmd_discover,
        "add": _cmd_add,
        "delete": _cmd_delete,
        "lookup": _cmd_lookup,
    }
    try:
        return handlers[args.command](args)
    except NotImplementedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
