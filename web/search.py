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

    category:radiology
    conference:"American Roentgen"
    remote:virtual
    reputation:big

Boolean operators (case-insensitive), with implicit AND between adjacent terms,
and parentheses for grouping::

    (virtual OR hybrid) AND reputation:big
    category:radiology NOT cost:*

Date comparisons on date fields accept ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``
and the operators ``> >= < <= =``::

    conference_dates:>=2026-06-01   # conference on/after that date
    abstract_due:<2026              # abstract deadline before 2026
    conference_dates:2026           # conference during 2026

Presence test (field is set / not set)::

    cost:*                  # has a cost recorded
    NOT conference_dates:*  # no conference date shown

The query fields mirror the table's column headers exactly: ``conference``,
``category``, ``location``, ``reputation``, ``remote``, ``cost``,
``abstract_due``, ``paper_due``, ``conference_dates``, ``conference_month``,
``abstract_month``, and ``paper_month`` (the month fields are integers 1-12,
derived from the displayed dates, e.g. ``conference_month:11`` or
``abstract_month:<=4``). Each date field
matches the value the column actually shows — the upcoming edition's date,
falling back to the prior edition's. A handful of legacy names (``name``,
``acronym``, ``remote_option``, ``abstract``, ``upcoming``, …) remain accepted
as hidden aliases so older shared query URLs keep working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple, Union

from sqlalchemy import and_, func, not_, or_

from conference_agent.database import ConferenceRow
from conference_agent.models import ConferenceTier, RemoteOption


class QueryError(Exception):
    """Raised when a query string cannot be tokenized or parsed."""


# --- Field registry --------------------------------------------------------

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
    "location": ("location",),
    "reputation": ("reputation",),
    "remote": ("remote_option",),
    "cost": ("cost",),
}

# Public date field → (upcoming column, prior column). Comparisons run against
# the value the column actually shows: the upcoming edition's date, falling back
# to the prior edition's (coalesce), matching the table's "upcoming ?? prior ?? —".
_DATE_FIELDS = {
    "abstract_due": ("upcoming_abstract_deadline", "prior_abstract_deadline"),
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
    "paper_month": "paper_month",
}

# Data type descriptor shown next to each field in the help panel. Categorical
# fields list their controlled vocabulary (derived from the enums so the help
# stays in sync); everything else is free text or a date.
_FIELD_TYPES = {
    "conference": "string",
    "category": "string",
    "location": "string",
    "reputation": "cat: " + ", ".join(t.value for t in ConferenceTier),
    "remote": "cat: " + ", ".join(o.value for o in RemoteOption if o is not RemoteOption.UNKNOWN),
    "cost": "string",
    "abstract_due": "date",
    "paper_due": "date",
    "conference_dates": "date",
    "conference_month": "int: 1-12",
    "abstract_month": "int: 1-12",
    "paper_month": "int: 1-12",
}

# Columns scanned by a bare (unscoped) keyword. Broader than the public fields so
# a loose keyword still reaches url/notes that have no column of their own.
_BARE_SEARCH_COLUMNS = (
    "acronym",
    "name",
    "category",
    "location",
    "reputation",
    "remote_option",
    "cost",
    "url",
    "notes",
)

# Legacy / convenience names accepted but not advertised, so older shared query
# URLs (and the internal column names) keep resolving to the public fields.
_ALIASES = {
    "name": "conference",
    "acronym": "conference",
    "remote_option": "remote",
    "tier": "reputation",
    "rep": "reputation",
    "abstract": "abstract_due",
    "deadline": "abstract_due",
    "upcoming_abstract_deadline": "abstract_due",
    "prior_abstract": "abstract_due",
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
    if key not in _TEXT_FIELDS and key not in _DATE_FIELDS and key not in _INT_FIELDS:
        raise QueryError(f"Unknown field: {name!r}")
    return key


def field_help() -> dict:
    """Return the queryable fields and their data types (for the UI help panel)."""
    order = list(_TEXT_FIELDS) + list(_DATE_FIELDS) + list(_INT_FIELDS)
    return {"fields": [{"field": f, "type": _FIELD_TYPES[f]} for f in order]}


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

_TOKEN_RE = re.compile(
    r"""
      \s+                                         # whitespace (skipped)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<field>[A-Za-z_]\w*)\s*:\s*
      (?P<op>>=|<=|>|<|=)?\s*
      (?P<val>"[^"]*"|\*|[^\s()]+)                # scoped field:value
    | (?P<quoted>"[^"]*")                         # quoted bare keyword
    | (?P<word>[^\s()":]+)                        # bare word / operator
    """,
    re.VERBOSE,
)

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
            op = m.group("op")
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


def _parse_date_bounds(value: str) -> Tuple[date, date]:
    """Return (lower, upper) inclusive date bounds for a partial date string."""
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


def _compile_int_term(term: Term):
    expr = getattr(ConferenceRow, _INT_FIELDS[term.field])
    try:
        value = int(term.value)
    except (TypeError, ValueError) as exc:
        raise QueryError(f"Invalid integer: {term.value!r}") from exc
    op = term.op or "="
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


def _compile_term(term: Term):
    # Presence test: field:* — the column shows a value.
    if term.presence:
        if term.field in _DATE_FIELDS:
            return _date_expr(term.field).isnot(None)
        if term.field in _INT_FIELDS:
            return getattr(ConferenceRow, _INT_FIELDS[term.field]).isnot(None)
        cols = _TEXT_FIELDS[term.field]
        return or_(*[getattr(ConferenceRow, c).isnot(None) for c in cols])

    # Bare keyword: substring across all text columns.
    if term.field is None:
        pattern = f"%{term.value}%"
        return or_(*[getattr(ConferenceRow, c).ilike(pattern) for c in _BARE_SEARCH_COLUMNS])

    # Scoped date field.
    if term.field in _DATE_FIELDS:
        return _compile_date_term(term)

    # Scoped integer field (month).
    if term.field in _INT_FIELDS:
        return _compile_int_term(term)

    # Scoped text field (one or more underlying columns).
    pattern = f"%{term.value}%"
    cols = _TEXT_FIELDS[term.field]
    return or_(*[getattr(ConferenceRow, c).ilike(pattern) for c in cols])


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
