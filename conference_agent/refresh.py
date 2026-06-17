"""Per-conference auto-check policy.

The standing refresh (:mod:`scripts.daily_update`) re-runs discovery for whole
fields on a fixed weekly/monthly cadence. This module adds a finer, per-series
decision: *which* conferences are worth re-checking right now, so discovery
calls are spent only when a new edition is plausibly about to be announced.

Policy (tuned by the constants in :mod:`conference_agent.config`):

- A series is anchored on its most recent known edition -- the upcoming start
  date if one is set, otherwise the prior start date.
- If a future upcoming edition is already on record, the series is considered
  **updated** and is not due: there is nothing new to find yet.
- Otherwise the anchor edition is in the past. The series becomes **due** once
  that edition is between ``CHECK_WINDOW_MIN_MONTHS`` and
  ``CHECK_WINDOW_MAX_MONTHS`` old -- old enough that next year's dates may be
  published soon, recent enough to assume the series is still active. Before the
  window opens it is too soon; after it closes (the edition is over a year old)
  checking stops, on the assumption the series is dead or infrequent.
- Inside the window the series is re-checked every ``RECHECK_INTERVAL_DAYS``
  days, measured from :attr:`ConferenceRow.last_checked`, until it is updated or
  the window closes.
- A row that has never been checked (``last_checked is None``) with no dates at
  all is due once, so freshly seeded rows get an initial pass.

Discovery covers a whole field per run, so the integration in ``daily_update``
selects the *fields* containing due conferences, refreshes those, and then
stamps ``last_checked`` across them via :func:`mark_categories_checked`.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from conference_agent.config import (
    CHECK_WINDOW_MAX_MONTHS,
    CHECK_WINDOW_MIN_MONTHS,
    DEFAULT_DATABASE_URL,
    RECHECK_INTERVAL_DAYS,
)
from conference_agent.database import ConferenceRow, get_engine
from conference_agent.models import normalize_categories


def _add_months(anchor: date, months: int) -> date:
    """Return ``anchor`` advanced by ``months`` calendar months.

    The day is clamped to the last valid day of the target month so that, e.g.,
    Aug 31 + 6 months lands on Feb 28 rather than raising.
    """
    index = anchor.month - 1 + months
    year = anchor.year + index // 12
    month = index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def is_due_for_check(row: ConferenceRow, today: Optional[date] = None) -> bool:
    """Whether ``row`` is due for an auto-check under the policy.

    See the module docstring for the full rule. ``today`` defaults to the
    current date; it is a parameter so the policy can be tested deterministically.
    """
    today = today or date.today()

    upcoming = row.upcoming_start_date
    # Already updated: a future upcoming edition is on record, so a re-check
    # would find nothing new. Stop until that edition itself passes.
    if upcoming is not None and upcoming >= today:
        return False

    # The anchor is the most recent edition we know about. With upcoming either
    # unset or already in the past, fall back to the prior edition's start.
    anchor = upcoming or row.prior_start_date
    if anchor is None:
        # No date to anchor on. Check once if we never have (initial seed fill);
        # otherwise there is nothing to schedule against.
        return row.last_checked is None

    window_open = _add_months(anchor, CHECK_WINDOW_MIN_MONTHS)
    window_close = _add_months(anchor, CHECK_WINDOW_MAX_MONTHS)
    if today < window_open or today > window_close:
        return False  # too soon to expect new dates, or past the one-year cutoff

    # Inside the window: honor the re-check interval since the last check.
    if row.last_checked is None:
        return True
    return (today - row.last_checked).days >= RECHECK_INTERVAL_DAYS


def due_categories(
    db_url: str = DEFAULT_DATABASE_URL, today: Optional[date] = None
) -> List[str]:
    """Distinct categories containing at least one due conference, sorted.

    Discovery runs per field, so this is the unit the scheduled refresh acts on.
    """
    today = today or date.today()
    engine = get_engine(db_url)
    with Session(engine) as session:
        rows = session.scalars(select(ConferenceRow))
        cats: set[str] = set()
        for row in rows:
            if is_due_for_check(row, today):
                # A row may carry several tags; each is a field worth refreshing.
                cats.update(normalize_categories(row.category))
    return sorted(cats)


def due_conference_ids(
    db_url: str = DEFAULT_DATABASE_URL, today: Optional[date] = None
) -> List[str]:
    """Ids of the conferences currently due for a check (for reporting/targeting)."""
    today = today or date.today()
    engine = get_engine(db_url)
    with Session(engine) as session:
        rows = session.scalars(select(ConferenceRow))
        return sorted(row.id for row in rows if is_due_for_check(row, today))


def mark_categories_checked(
    categories: List[str],
    db_url: str = DEFAULT_DATABASE_URL,
    today: Optional[date] = None,
) -> int:
    """Stamp ``last_checked = today`` on every row in ``categories``.

    A discovery run covers a whole field, so after refreshing a category every
    row in it has just been checked. Recording that on all of them (not only the
    ones that triggered the run) prevents redundant re-runs before the next
    interval elapses. Returns the number of rows stamped.
    """
    today = today or date.today()
    cats = {c for c in categories}
    if not cats:
        return 0
    engine = get_engine(db_url)
    with Session(engine) as session:
        # Substring match per tag: a row whose category column lists several tags
        # (e.g. "radiology, pediatrics") was covered if any refreshed field
        # appears in it, so an exact ``IN`` match would miss multi-tag rows.
        conds = [ConferenceRow.category.ilike(f"%{c}%") for c in cats]
        rows = list(
            session.scalars(select(ConferenceRow).where(or_(*conds)))
        )
        for row in rows:
            row.last_checked = today
        session.commit()
        return len(rows)
