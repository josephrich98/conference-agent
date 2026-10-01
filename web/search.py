"""Boolean search query language for the conference table.

Parses a compact boolean query string and compiles it to a SQLAlchemy filter
expression over :class:`conference_agent.database.ConferenceRow`. All matching is
done through SQLAlchemy expressions (bound parameters), so user input is never
interpolated into raw SQL.

Query syntax
------------
Bare keywords match (case-insensitive substring) across all text fields::

    radiology imaging

Scoped field match::

    category:medicine
    subcategory:radiology
    conference:"American Roentgen"
    remote:virtual
    size:large

Boolean operators (case-insensitive), with implicit AND between adjacent terms,
and parentheses for grouping::

    (virtual OR hybrid) AND size:large
    subcategory:radiology NOT cost:*

Date comparisons on date fields accept ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, or
``today`` / ``now`` (the current date), and the operators ``> >= < <= =`` (``=>`` / ``=<`` are accepted as typos for
``>=`` / ``<=``). The operator may follow a colon or attach directly to the
field::

    conference_dates:>=2026-06-01   # conference on/after that date
    abstract_due:<2026              # abstract deadline before 2026
    conference_dates:2026           # conference during 2026
    abstract_due:>=today            # abstract deadline still ahead
    abstract_month>=6               # colon optional before a comparison operator

Month fields (``conference_month``, ``abstract_month``, ``paper_month``) accept
an integer ``1-12``, a month name / 3-letter abbreviation (case-insensitive), or
``today`` / ``now`` for the current month::

    abstract_month:>=June           # abstract deadline in June or later
    conference_month:nov            # conference in November

Size compares by rank (small < medium < large < massive) when given an
operator; without one it is the usual substring match::

    size>=large                     # large or massive
    size:<medium                    # small

``attendance`` is the annual attendance figure; it accepts the same operators
(defaulting to ``=``), and values may use thousands separators or a ``k``
suffix::

    attendance>=5000                # at least 5,000 attendees
    attendance:<1.5k                # under 1,500

Presence test (field is set / not set)::

    cost:*                  # has a cost recorded
    NOT conference_dates:*  # no conference date shown

The query fields mirror the table's column headers exactly: ``conference``,
``category`` (one of the ten top-level buckets), ``subcategory`` (the specific
field), ``format`` (any of abstract / paper / poster / oral),
``location``, ``size``, ``attendance``, ``remote``, ``cost``, ``registration`` (free text — a
substring match), ``deadline_time`` (free text — the time of day and zone
submissions close, e.g. ``deadline_time:AoE``), ``abstract_due``, ``late_abstract_due`` (the second, later
abstract deadline some series publish — a poster-only deadline or a
late-breaking round), ``paper_due``, ``conference_dates``,
``conference_month``, ``abstract_month``, ``late_abstract_month``, and
``paper_month`` (the month fields
are integers 1-12, derived from the displayed dates, e.g. ``conference_month:11``,
``abstract_month:<=April``, or ``abstract_month>=June``). Each date field
matches the value the column actually shows — the upcoming edition's date,
falling back to the prior edition's. A handful of legacy names (``name``,
``acronym``, ``remote_option``, ``abstract``, ``upcoming``, …) remain accepted
as hidden aliases so older shared query URLs keep working.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple, Union

from sqlalchemy import and_, case, func, not_, or_

from conference_agent.database import ConferenceRow
from conference_agent.models import CATEGORIES, CONFERENCE_FORMATS, ConferenceSize, RemoteOption


class QueryError(Exception):
    """Raised when a query string cannot be tokenized or parsed."""


# --- Field registry --------------------------------------------------------

# Columns returned and exported by the API / static snapshot, in display order.
RESULT_COLUMNS = [
    "id",
    "acronym",
    "name",
    "category",
    "subcategory",
    "format",
    "size",
    "attendance",
    "attendance_year",
    "remote_option",
    "url",
    "abstract_month",
    "upcoming_abstract_deadline",
    "late_abstract_month",
    "upcoming_late_abstract_deadline",
    "paper_month",
    "upcoming_paper_deadline",
    "deadline_time",
    "conference_month",
    "upcoming_start_date",
    "upcoming_end_date",
    "upcoming_location",
    "prior_location",
    "stable_location",
    "upcoming_registration",
    "prior_registration",
    "upcoming_cost",
    "prior_cost",
    "prior_abstract_deadline",
    "prior_late_abstract_deadline",
    "prior_paper_deadline",
    "prior_start_date",
    "prior_end_date",
    "notes",
]

# The queryable fields exposed to users mirror the table's column headers exactly
# (see COLUMNS in web/static/index.html), so the search box and the displayed
# table agree on names. Each public field maps to the underlying ConferenceRow
# column(s); users never need to know the internal names.

# Public text field → underlying column(s). A scoped match is a case-insensitive
# substring OR-ed across the listed columns. The labels mirror the column
# headers ("Conference", "Category", …).
_TEXT_FIELDS = {
    "conference": ("acronym", "name"),
    "category": ("category",),
    "subcategory": ("subcategory",),
    "format": ("format",),
    # Location and cost are per edition: a substring match across both slots,
    # like registration.
    "location": ("upcoming_location", "prior_location"),
    "size": ("size",),
    "remote": ("remote_option",),
    "cost": ("upcoming_cost", "prior_cost"),
    # Registration is free text (windows like "Early bird: ...; Regular: ..."), so
    # it is a substring match across the upcoming and prior registration columns,
    # not a date comparison.
    "registration": ("upcoming_registration", "prior_registration"),
    # Time of day (+ zone) submissions close -- free text, substring match
    # (e.g. ``deadline_time:AoE``).
    "deadline_time": ("deadline_time",),
}

# Public date field → (upcoming column, prior column). Comparisons run against
# the value the column actually shows: the upcoming edition's date, falling back
# to the prior edition's (coalesce), matching the table's "upcoming ?? prior ?? —".
_DATE_FIELDS = {
    "abstract_due": ("upcoming_abstract_deadline", "prior_abstract_deadline"),
    # The second, later abstract deadline some series publish (a poster-only
    # deadline, or a late-breaking / late-poster round). Its own field rather
    # than a second value on ``abstract_due`` so both stay sortable dates.
    "late_abstract_due": (
        "upcoming_late_abstract_deadline",
        "prior_late_abstract_deadline",
    ),
    "paper_due": ("upcoming_paper_deadline", "prior_paper_deadline"),
    "conference_dates": ("upcoming_start_date", "prior_start_date"),
}

# Public integer field → underlying (SQL-computed) column. The month fields are
# derived from the displayed date (see ``database.ConferenceRow``), so they sort
# and filter by season independent of year. Comparisons accept the same operators
# as date fields (``> >= < <= =``), defaulting to ``=``.
_INT_FIELDS = {
    "conference_month": "conference_month",
    "abstract_month": "abstract_month",
    "late_abstract_month": "late_abstract_month",
    "paper_month": "paper_month",
}

# Public numeric field -> underlying column. Same operators as the month fields;
# values are counts (``5000``, ``5,000``, or ``5k``).
_NUMBER_FIELDS = {
    "attendance": "attendance",
}

# Size buckets ranked by magnitude, for comparisons such as ``size>=large``.
_SIZE_RANK = {"small": 1, "medium": 2, "large": 3, "massive": 4}

# Data type descriptor shown next to each field in the help panel. Categorical
# fields list their controlled vocabulary (derived from the enums so the help
# stays in sync); everything else is free text or a date.
_FIELD_TYPES = {
    "conference": "string",
    "category": "cat: " + ", ".join(CATEGORIES),
    "subcategory": "string",
    "format": "cat: " + ", ".join(CONFERENCE_FORMATS),
    "location": "string",
    "size": "cat: " + ", ".join(t.value for t in ConferenceSize),
    "attendance": "int: count, e.g. 5000 or 5k",
    "remote": "cat: " + ", ".join(o.value for o in RemoteOption),
    "cost": "string",
    "registration": "string",
    "deadline_time": "string",
    "abstract_due": "date",
    "late_abstract_due": "date",
    "paper_due": "date",
    "conference_dates": "date",
    "conference_month": "int: 1-12 or month name",
    "abstract_month": "int: 1-12 or month name",
    "late_abstract_month": "int: 1-12 or month name",
    "paper_month": "int: 1-12 or month name",
}

# Columns scanned by a bare (unscoped) keyword. Broader than the public fields so
# a loose keyword still reaches url/notes that have no column of their own.
_BARE_SEARCH_COLUMNS = (
    "acronym",
    "name",
    "category",
    "subcategory",
    "format",
    "upcoming_location",
    "prior_location",
    "size",
    "remote_option",
    "upcoming_cost",
    "prior_cost",
    "upcoming_registration",
    "prior_registration",
    "deadline_time",
    "url",
    "notes",
)

# Legacy / convenience names accepted but not advertised, so older shared query
# URLs (and the internal column names) keep resolving to the public fields.
_ALIASES = {
    "name": "conference",
    "acronym": "conference",
    "formats": "format",
    "remote_option": "remote",
    "abstract": "abstract_due",
    "deadline": "abstract_due",
    "upcoming_abstract_deadline": "abstract_due",
    "prior_abstract": "abstract_due",
    "late_abstract": "late_abstract_due",
    "late_breaking": "late_abstract_due",
    "poster_due": "late_abstract_due",
    "upcoming_late_abstract_deadline": "late_abstract_due",
    "prior_late_abstract": "late_abstract_due",
    "paper": "paper_due",
    "upcoming_paper_deadline": "paper_due",
    "prior_paper": "paper_due",
    "upcoming": "conference_dates",
    "date": "conference_dates",
    "upcoming_start_date": "conference_dates",
    "prior_start": "conference_dates",
    # The single submission-month column was split into abstract/paper months;
    # keep older shared query URLs resolving to the abstract (earlier) deadline.
    "submission_month": "abstract_month",
}


def _resolve_field(name: str) -> str:
    key = name.lower()
    key = _ALIASES.get(key, key)
    if (
        key not in _TEXT_FIELDS
        and key not in _DATE_FIELDS
        and key not in _INT_FIELDS
        and key not in _NUMBER_FIELDS
    ):
        raise QueryError(f"Unknown field: {name!r}")
    return key


_FIELD_DESCRIPTIONS = {
    "conference": "conference name and hyperlink to its website",
    "category": "broad top-level field, derived from the subcategories",
    "subcategory": "specific field(s) the conference covers; one or more per series",
    "format": "submission types accepted (abstract, paper, poster, oral)",
    "location": "city and country of the upcoming (or most recent) meeting",
    "size": "bucket from annual attendance: massive 10,000+, large 1,000-9,999, medium 250-999, small under 250; "
    "compare by rank, e.g. size>=large",
    "attendance": "most recent annual attendance, e.g. attendance>=5000",
    "remote": "whether the meeting is in-person, virtual, or hybrid",
    "cost": "registration fee or fee range of the upcoming (or most recent) meeting",
    "registration": "registration windows, e.g. early bird and regular periods",
    "deadline_time": "time of day and time zone that submissions close",
    "abstract_due": "earliest (primary) abstract deadline",
    "late_abstract_due": "later abstract deadline: poster-only or late-breaking round",
    "paper_due": "full-paper submission deadline",
    "conference_dates": "start and end dates of the meeting",
    "conference_month": "month of the conference (1-12), for sorting by season",
    "abstract_month": "month of the abstract deadline (1-12)",
    "late_abstract_month": "month of the late abstract deadline (1-12)",
    "paper_month": "month of the paper deadline (1-12)",
}


def field_help() -> dict:
    """Return the queryable fields, data types, and descriptions (for the UI help panel)."""
    order = list(_TEXT_FIELDS) + list(_NUMBER_FIELDS) + list(_DATE_FIELDS) + list(_INT_FIELDS)
    return {
        "fields": [
            {"field": f, "type": _FIELD_TYPES[f], "description": _FIELD_DESCRIPTIONS[f]}
            for f in order
        ]
    }


# --- AST --------------------------------------------------------------------


@dataclass
class Term:
    """A single match: a bare keyword, a scoped value, or a presence test."""

    field: Optional[str]  # None → bare keyword across all text fields
    op: Optional[str]  # comparison operator for date fields
    value: str
    presence: bool = False  # True → ``field:*``


@dataclass
class NotOp:
    child: "Node"


@dataclass
class BoolOp:
    op: str  # "AND" or "OR"
    children: List["Node"]


Node = Union[Term, NotOp, BoolOp]


# --- Tokenizer --------------------------------------------------------------

# A comparison operator may separate field and value either after a colon
# (``abstract_month:>=6``) or directly (``abstract_month>=6``). ``=>`` / ``=<``
# are accepted as common typos for ``>=`` / ``<=``.
_OP_ALT = r">=|<=|=>|=<|>|<|="

_TOKEN_RE = re.compile(
    rf"""
      \s+                                         # whitespace (skipped)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<field>[A-Za-z_]\w*)                     # scoped field:value
      (?: \s*:\s*(?P<colon_op>{_OP_ALT})?         #   field: [op] value
        | \s*(?P<bare_op>{_OP_ALT})               #   field op value (no colon)
      )\s*
      (?P<val>"[^"]*"|\*|[^\s()]+)
    | (?P<quoted>"[^"]*")                         # quoted bare keyword
    | (?P<word>[^\s()":]+)                        # bare word / operator
    """,
    re.VERBOSE,
)

# Comparison-operator typos normalized to their canonical form.
_OP_NORMALIZE = {"=>": ">=", "=<": "<="}

_OPERATORS = {"AND", "OR", "NOT"}


@dataclass
class _Tok:
    kind: str  # "term" | "and" | "or" | "not" | "lparen" | "rparen"
    term: Optional[Term] = None


def _tokenize(query: str) -> List[_Tok]:
    tokens: List[_Tok] = []
    pos = 0
    for m in _TOKEN_RE.finditer(query):
        if m.start() != pos:
            raise QueryError(f"Unexpected character at position {pos}")
        pos = m.end()

        if m.lastgroup is None and m.group().strip() == "":
            continue  # whitespace
        if m.group("lparen"):
            tokens.append(_Tok("lparen"))
        elif m.group("rparen"):
            tokens.append(_Tok("rparen"))
        elif m.group("field"):
            field = _resolve_field(m.group("field"))
            op = m.group("colon_op") or m.group("bare_op")
            op = _OP_NORMALIZE.get(op, op)
            raw = m.group("val")
            if raw == "*":
                tokens.append(_Tok("term", Term(field=field, op=None, value="", presence=True)))
            else:
                value = raw[1:-1] if raw.startswith('"') else raw
                tokens.append(_Tok("term", Term(field=field, op=op, value=value)))
        elif m.group("quoted") is not None:
            value = m.group("quoted")[1:-1]
            tokens.append(_Tok("term", Term(field=None, op=None, value=value)))
        elif m.group("word") is not None:
            word = m.group("word")
            upper = word.upper()
            if upper in _OPERATORS:
                tokens.append(_Tok(upper.lower()))
            else:
                tokens.append(_Tok("term", Term(field=None, op=None, value=word)))

    if pos != len(query):
        raise QueryError(f"Unexpected character at position {pos}")
    return tokens


# --- Parser (recursive descent) --------------------------------------------


class _Parser:
    def __init__(self, tokens: List[_Tok]):
        self.tokens = tokens
        self.i = 0

    def _peek(self) -> Optional[_Tok]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _next(self) -> _Tok:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self) -> Optional[Node]:
        if not self.tokens:
            return None
        node = self._parse_or()
        if self._peek() is not None:
            raise QueryError("Unbalanced parentheses or trailing tokens")
        return node

    def _parse_or(self) -> Node:
        children = [self._parse_and()]
        while self._peek() and self._peek().kind == "or":
            self._next()
            children.append(self._parse_and())
        return children[0] if len(children) == 1 else BoolOp("OR", children)

    def _parse_and(self) -> Node:
        children = [self._parse_not()]
        while True:
            tok = self._peek()
            if tok is None or tok.kind in ("or", "rparen"):
                break
            if tok.kind == "and":
                self._next()  # explicit AND
            # else implicit AND between adjacent terms
            children.append(self._parse_not())
        return children[0] if len(children) == 1 else BoolOp("AND", children)

    def _parse_not(self) -> Node:
        if self._peek() and self._peek().kind == "not":
            self._next()
            return NotOp(self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> Node:
        tok = self._peek()
        if tok is None:
            raise QueryError("Unexpected end of query")
        if tok.kind == "lparen":
            self._next()
            node = self._parse_or()
            closing = self._peek()
            if closing is None or closing.kind != "rparen":
                raise QueryError("Missing closing parenthesis")
            self._next()
            return node
        if tok.kind == "term":
            self._next()
            return tok.term
        raise QueryError(f"Unexpected token: {tok.kind}")


# --- Compiler (AST → SQLAlchemy) -------------------------------------------


# Values that stand for the current date (date fields) or month (month fields).
_NOW_WORDS = ("today", "now")


def _parse_date_bounds(value: str) -> Tuple[date, date]:
    """Return (lower, upper) inclusive date bounds for a partial date string.

    ``today`` / ``now`` (case-insensitive) mean the current date.
    """
    if value.strip().lower() in _NOW_WORDS:
        today = date.today()
        return today, today
    parts = value.split("-")
    try:
        if len(parts) == 1:  # YYYY
            year = int(parts[0])
            return date(year, 1, 1), date(year, 12, 31)
        if len(parts) == 2:  # YYYY-MM
            year, month = int(parts[0]), int(parts[1])
            if month == 12:
                upper = date(year, 12, 31)
            else:
                upper = date(year, month + 1, 1).replace(day=1)
                from datetime import timedelta

                upper = upper - timedelta(days=1)
            return date(year, month, 1), upper
        if len(parts) == 3:  # YYYY-MM-DD
            d = date(int(parts[0]), int(parts[1]), int(parts[2]))
            return d, d
    except ValueError as exc:
        raise QueryError(f"Invalid date: {value!r}") from exc
    raise QueryError(f"Invalid date: {value!r}")


def _date_expr(field: str):
    """The displayed date for a public date field: upcoming, falling back to prior."""
    upcoming, prior = _DATE_FIELDS[field]
    return func.coalesce(getattr(ConferenceRow, upcoming), getattr(ConferenceRow, prior))


def _compile_date_term(term: Term):
    expr = _date_expr(term.field)
    lower, upper = _parse_date_bounds(term.value)
    op = term.op or "="
    if op == "=":
        return and_(expr.isnot(None), expr >= lower, expr <= upper)
    if op == ">":
        return and_(expr.isnot(None), expr > upper)
    if op == ">=":
        return and_(expr.isnot(None), expr >= lower)
    if op == "<":
        return and_(expr.isnot(None), expr < lower)
    if op == "<=":
        return and_(expr.isnot(None), expr <= upper)
    raise QueryError(f"Unsupported operator: {op}")


_MONTH_NAMES = {
    name: num
    for num, full in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
    for name in (full, full[:3])
}


def _parse_month(value: str) -> int:
    """Parse a month field value: an integer 1-12, a (abbreviated) month name, or
    ``today`` / ``now`` for the current month."""
    token = value.strip().lower()
    if token in _NOW_WORDS:
        return date.today().month
    if token.isdigit():
        num = int(token)
        if 1 <= num <= 12:
            return num
        raise QueryError(f"Month out of range (1-12): {value!r}")
    if token in _MONTH_NAMES:
        return _MONTH_NAMES[token]
    raise QueryError(f"Invalid month: {value!r} (use 1-12 or a month name)")


def _compile_int_term(term: Term):
    expr = getattr(ConferenceRow, _INT_FIELDS[term.field])
    return _compare(expr, term.op, _parse_month(term.value))


_COUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)(k?)$")


def _parse_count(value: str) -> int:
    """Parse a count: an integer, optionally with thousands separators or a ``k`` suffix."""
    m = _COUNT_RE.match(value.strip().lower().replace(",", ""))
    if not m or (not m.group(2) and "." in m.group(1)):
        raise QueryError(f"Invalid number: {value!r} (e.g. 5000, 5,000, or 5k)")
    number = float(m.group(1)) * (1000 if m.group(2) else 1)
    return int(round(number))


def _compare(expr, op: Optional[str], value):
    """``expr <op> value`` (default ``=``), false when ``expr`` is NULL."""
    op = op or "="
    ops = {
        "=": expr == value,
        ">": expr > value,
        ">=": expr >= value,
        "<": expr < value,
        "<=": expr <= value,
    }
    if op not in ops:
        raise QueryError(f"Unsupported operator: {op}")
    return and_(expr.isnot(None), ops[op])


def _compile_size_rank_term(term: Term):
    rank = _SIZE_RANK.get(term.value.strip().lower())
    if rank is None:
        raise QueryError(
            f"Invalid size: {term.value!r} (use {', '.join(_SIZE_RANK)})"
        )
    return _compare(case(_SIZE_RANK, value=ConferenceRow.size), term.op, rank)


def _compile_term(term: Term):
    # Presence test: field:* — the column shows a value.
    if term.presence:
        if term.field in _DATE_FIELDS:
            return _date_expr(term.field).isnot(None)
        if term.field in _INT_FIELDS:
            return getattr(ConferenceRow, _INT_FIELDS[term.field]).isnot(None)
        if term.field in _NUMBER_FIELDS:
            return getattr(ConferenceRow, _NUMBER_FIELDS[term.field]).isnot(None)
        cols = _TEXT_FIELDS[term.field]
        return or_(*[getattr(ConferenceRow, c).isnot(None) for c in cols])

    # Bare keyword: substring across all text columns.
    if term.field is None:
        pattern = f"%{term.value}%"
        return or_(*[_ilike(c, pattern) for c in _BARE_SEARCH_COLUMNS])

    # Scoped date field.
    if term.field in _DATE_FIELDS:
        return _compile_date_term(term)

    # Scoped integer field (month).
    if term.field in _INT_FIELDS:
        return _compile_int_term(term)

    # Scoped numeric field (attendance).
    if term.field in _NUMBER_FIELDS:
        expr = getattr(ConferenceRow, _NUMBER_FIELDS[term.field])
        return _compare(expr, term.op, _parse_count(term.value))

    # Size with a comparison operator compares by rank (size>=large).
    if term.field == "size" and term.op:
        return _compile_size_rank_term(term)

    # Scoped text field (one or more underlying columns).
    pattern = f"%{term.value}%"
    cols = _TEXT_FIELDS[term.field]
    return or_(*[_ilike(c, pattern) for c in cols])


def _ilike(column: str, pattern: str):
    # Coalesce so a NULL cell is a plain non-match: a bare ``NULL LIKE ...`` is
    # NULL, which ``NOT`` would keep NULL and silently drop the row (the browser
    # search treats a blank cell as not matching, so ``NOT remote:virtual`` keeps it).
    return func.coalesce(getattr(ConferenceRow, column), "").ilike(pattern)


def _compile(node: Node):
    if isinstance(node, Term):
        return _compile_term(node)
    if isinstance(node, NotOp):
        return not_(_compile(node.child))
    if isinstance(node, BoolOp):
        compiled = [_compile(c) for c in node.children]
        return and_(*compiled) if node.op == "AND" else or_(*compiled)
    raise QueryError("Malformed query tree")


def build_filter(query: str):
    """Compile a query string into a SQLAlchemy filter, or ``None`` if empty."""
    if query is None or not query.strip():
        return None
    tokens = _tokenize(query)
    node = _Parser(tokens).parse()
    if node is None:
        return None
    return _compile(node)


# ---------------------------------------------------------------------------
# Keyword fallback (Python port of ``keywordSearch`` in web/static/search.js).
#
# The table runs the exact boolean search first; when it matches nothing, or the
# text does not parse, it falls back to this forgiving, relevance-ranked keyword
# match (prefix / stem / typo tolerant, filler words dropped, rows matching more
# terms first). Ported so ``conference-agent lookup`` behaves like the site;
# tests/test_keyword_search.py pins the two implementations together via Node.
# It works on serialized row dicts (the shape of ``/api/search`` and the static
# snapshot), not SQL, so every constant below mirrors its JS counterpart.
# ---------------------------------------------------------------------------

# Row column -> weight of a hit in that column.
_KEYWORD_FIELDS = (
    ("acronym", 4),
    ("name", 3),
    ("subcategory", 3),
    ("category", 2),
    ("upcoming_location", 2),
    ("prior_location", 2),
    ("format", 1),
    ("remote_option", 1),
    ("size", 1),
    ("upcoming_cost", 0.5),
    ("prior_cost", 0.5),
    ("deadline_time", 0.5),
    ("upcoming_registration", 0.5),
    ("prior_registration", 0.5),
    ("url", 0.5),
    ("notes", 0.5),
)

# Long free-text columns where typo-tolerant matching mostly produces noise.
_KEYWORD_EXACT_ONLY = {"notes", "url", "upcoming_registration", "prior_registration"}

# Displayed-date columns a four-digit year is checked against.
_KEYWORD_DATE_COLUMNS = (
    "upcoming_start_date",
    "prior_start_date",
    "upcoming_abstract_deadline",
    "prior_abstract_deadline",
    "upcoming_late_abstract_deadline",
    "prior_late_abstract_deadline",
    "upcoming_paper_deadline",
    "prior_paper_deadline",
)

_KEYWORD_MONTH_COLUMNS = ("conference_month", "abstract_month", "late_abstract_month", "paper_month")

# Filler words dropped from a keyword query (plus the boolean operators).
_STOPWORDS = {
    "a", "an", "the", "and", "or", "not", "of", "in", "on", "at", "for", "to",
    "with", "without", "by", "from", "about", "as", "is", "are", "be", "that",
    "this", "these", "those", "which", "what", "where", "when", "who", "i", "me",
    "my", "we", "our", "you", "your", "any", "all", "some", "find", "show", "list",
    "give", "want", "looking", "look", "need", "near", "held", "conference",
    "conferences", "meeting", "meetings", "event", "events",
}  # fmt: skip

# Common shorthand -> the wording used in the data. A term matches if it or any
# of its expansions matches.
_KEYWORD_SYNONYMS = {
    "ai": ["artificial intelligence"],
    "ml": ["machine learning"],
    "nlp": ["natural language processing"],
    "cs": ["computer science"],
    "online": ["virtual", "hybrid"],
    "remote": ["virtual", "hybrid"],
    "big": ["large", "massive"],
    "major": ["large", "massive"],
    "huge": ["massive"],
    "enormous": ["massive"],
    "tiny": ["small"],
}

_KNOWN_FIELD_NAMES = {
    *_TEXT_FIELDS, *_DATE_FIELDS, *_INT_FIELDS, *_NUMBER_FIELDS, *_ALIASES,
}  # fmt: skip

_YEAR_RE = re.compile(r"(19|20)\d\d")
_DIGITS_RE = re.compile(r"[0-9]+")


def _fold_text(s) -> str:
    decomposed = unicodedata.normalize("NFD", str(s))
    return "".join(ch for ch in decomposed if not 0x300 <= ord(ch) <= 0x36F).lower()


def _split_words(s) -> List[str]:
    return [w for w in re.split(r"[\W_]+", _fold_text(s)) if w]


def _edit_distance(a: str, b: str, max_dist: int) -> int:
    """Optimal-string-alignment distance (a transposition costs 1), with an
    early exit once the distance is known to exceed ``max_dist``."""
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    prev2 = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        row_min = i
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur.append(v)
            row_min = min(row_min, v)
        if row_min > max_dist:
            return max_dist + 1
        prev2, prev = prev, cur
    return prev[len(b)]


def _common_prefix_length(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _word_quality(term: str, word: str, fuzzy: bool) -> float:
    """How well a single-word term matches one word of the row text, 0 to 1."""
    if term == word:
        return 1
    if len(term) < 3:
        return 0
    if word.startswith(term):
        return 0.9
    shared = _common_prefix_length(term, word)
    if shared >= max(4, math.ceil(0.75 * min(len(term), len(word)))):
        return 0.8
    if len(term) >= 4 and term in word:
        return 0.7
    if fuzzy and len(term) >= 6:
        max_dist = 2 if len(term) >= 9 else 1
        if _edit_distance(term, word, max_dist) <= max_dist:
            return 0.6
    return 0


def _keyword_index(row: dict) -> list:
    index = []
    for col, weight in _KEYWORD_FIELDS:
        value = row.get(col)
        if value is None or value == "":
            continue
        words = _split_words(value)
        index.append(
            (weight, col not in _KEYWORD_EXACT_ONLY, f" {' '.join(words)} ", list(dict.fromkeys(words)))
        )
    return index


def keyword_terms(query: str) -> List[str]:
    """Split a query into keyword terms: quoted phrases stay whole, filler words drop."""
    terms: List[str] = []

    def add(t: str) -> None:
        if t not in terms:
            terms.append(t)

    def phrase(m: re.Match) -> str:
        words = _split_words(m.group(1))
        if words:
            add(" ".join(words))
        return " "

    rest = re.sub(r'"([^"]*)"', phrase, str(query or ""))
    rest = re.sub(
        r"([A-Za-z_]\w*)\s*:",
        lambda m: " " if m.group(1).lower() in _KNOWN_FIELD_NAMES else f" {m.group(1)} ",
        rest,
        flags=re.ASCII,
    )
    for word in _split_words(rest):
        if word in _STOPWORDS:
            continue
        if _DIGITS_RE.fullmatch(word) and not _YEAR_RE.fullmatch(word):
            continue  # the "06" of a date
        if len(word) < 2:
            continue
        add(word)
    return terms


def _term_score(term: str, row: dict, index: list) -> float:
    """Best weighted match of one term anywhere in the row, 0 if none."""
    best = 0
    for alt in (term, *_KEYWORD_SYNONYMS.get(term, ())):
        is_phrase = " " in alt
        for weight, fuzzy, text, words in index:
            if weight <= best:
                continue  # cannot beat the best hit so far
            q = 0
            if is_phrase:
                if f" {alt} " in text:
                    q = 1
                elif alt in text:
                    q = 0.8
            else:
                for word in words:
                    q = max(q, _word_quality(alt, word, fuzzy))
                    if q == 1:
                        break
            best = max(best, q * weight)
    if _YEAR_RE.fullmatch(term) and any(
        row.get(c) is not None and str(row[c]).startswith(term) for c in _KEYWORD_DATE_COLUMNS
    ):
        best = max(best, 1)
    month = _MONTH_NAMES.get(term)
    if month is not None and any(row.get(c) == month for c in _KEYWORD_MONTH_COLUMNS):
        best = max(best, 1)
    return best


def keyword_search(query: str, rows: List[dict]) -> List[dict]:
    """Forgiving keyword ranking over serialized ``rows``.

    Returns the rows matching at least half as many of the query's terms as the
    best row does, most relevant first (more terms matched, then a higher
    score, with rarer terms weighing more). Empty when the query has no usable
    terms. Mirrors ``keywordSearch`` in web/static/search.js.
    """
    terms = keyword_terms(query)
    if not terms:
        return []
    per_row = []
    for row in rows:
        index = _keyword_index(row)
        per_row.append([_term_score(t, row, index) for t in terms])
    idf = []
    for i in range(len(terms)):
        df = sum(1 for scores in per_row if scores[i] > 0)
        idf.append(math.log(1 + len(rows) / df) if df else 0)
    hits = []
    for row, scores in zip(rows, per_row):
        matched = sum(1 for s in scores if s > 0)
        if matched:
            hits.append((row, matched, sum(s * w for s, w in zip(scores, idf))))
    if not hits:
        return []
    min_matched = math.ceil(max(h[1] for h in hits) / 2)
    hits = [h for h in hits if h[1] >= min_matched]
    hits.sort(key=lambda h: (-h[1], -h[2]))
    return [h[0] for h in hits]
