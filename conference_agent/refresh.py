"""Per-conference auto-check policy.

Two schedules live here.

**Due (per field).** The standing refresh (:mod:`scripts.daily_update`) re-runs
discovery for whole fields. :func:`is_due_for_check` adds a finer, per-series
decision: *which* conferences are worth re-checking right now, so discovery
calls are spent only when a new edition is plausibly about to be announced.

- A series is anchored on its most recent known edition (the upcoming one if any
  of its dates are on record, otherwise the prior one), dated by that edition's
  earliest submission deadline, or by its start date when no deadline is known.
- If any date of the upcoming edition is still in the future, the series is
  considered **updated** and is not due: there is nothing new to find yet.
- Otherwise the series becomes **due** once the anchor is between
  ``CHECK_WINDOW_MIN_MONTHS`` and ``CHECK_WINDOW_MAX_MONTHS`` old -- old enough
  that next year's dates may be published soon, recent enough to assume the
  series is still active. Outside that window it is not checked.
- Inside the window the series is re-checked every ``RECHECK_INTERVAL_DAYS``
  days, measured from :attr:`ConferenceRow.last_checked`, until it is updated or
  the window closes.
- A row that has never been checked (``last_checked is None``) with no dates at
  all is due once, so freshly seeded rows get an initial pass.

Discovery covers a whole field per run, so the integration in ``daily_update``
selects the *fields* containing due conferences, refreshes those, and then
stamps ``last_checked`` across them via :func:`mark_subcategories_checked`.

**Watch (per series, page-gated).** :func:`run_watch` is meant to run daily. It
sorts series into tiers (:func:`watch_tier`: deadlines within two weeks either
side are checked daily; a deadline or meeting within the next month, series
whose last edition ended less than ``CHECK_WINDOW_MIN_MONTHS`` ago, and series in
the due window above, every two weeks), runs the agent-free page check
(:mod:`conference_agent.page_watch`) on each series whose tier makes it due, and
re-researches -- through the targeted :func:`discover.refresh_conferences`, not
a whole-field run -- only the series whose pages changed, could not be read and
have not been researched in ``RECHECK_INTERVAL_DAYS``, or have gone
``WATCH_BACKSTOP_DAYS`` without research. See ``config.py`` for the knobs.
"""

from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from conference_agent.config import (
    CHECK_WINDOW_MAX_MONTHS,
    CHECK_WINDOW_MIN_MONTHS,
    DEFAULT_DATABASE_URL,
    RECHECK_INTERVAL_DAYS,
    WATCH_BACKSTOP_DAYS,
    WATCH_BATCH_SIZE,
    WATCH_DAILY_WINDOW_DAYS,
    WATCH_MAX_AGENT_PER_RUN,
    WATCH_SOON_WINDOW_DAYS,
)
from conference_agent.database import (
    ConferenceRow,
    _row_to_model,
    apply_refreshed_conferences,
    attendance_hints_for,
    get_engine,
    known_attendance_sources,
    roll_past_editions,
)
from conference_agent.models import Conference, normalize_subcategories
from conference_agent.page_watch import PageCheck


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


_UPCOMING_DATE_FIELDS = (
    "upcoming_abstract_deadline",
    "upcoming_late_abstract_deadline",
    "upcoming_paper_deadline",
    "upcoming_start_date",
    "upcoming_end_date",
)
_UPCOMING_DEADLINE_FIELDS = (
    "upcoming_abstract_deadline",
    "upcoming_late_abstract_deadline",
    "upcoming_paper_deadline",
)


def _has_future_edition(row: ConferenceRow, today: date) -> bool:
    """Whether any date of the upcoming edition is still ahead of ``today``."""
    return any(
        (d := getattr(row, f)) is not None and d >= today for f in _UPCOMING_DATE_FIELDS
    )


def edition_anchor(row: ConferenceRow) -> Optional[date]:
    """The date the staleness window is measured from.

    The most recent known edition is the upcoming one when any of its dates are
    on record, otherwise the prior one. That edition is dated by its earliest
    submission deadline (abstract or paper) -- the point after which its call is
    over -- or by its start date when no deadline is known. ``None`` when the
    row carries no dates at all.
    """
    has_upcoming = any(getattr(row, f) is not None for f in _UPCOMING_DATE_FIELDS)
    edition = "upcoming" if has_upcoming else "prior"
    deadlines = [
        d
        for d in (
            getattr(row, f"{edition}_abstract_deadline"),
            getattr(row, f"{edition}_paper_deadline"),
        )
        if d is not None
    ]
    if deadlines:
        return min(deadlines)
    return getattr(row, f"{edition}_start_date")


def in_stale_window(row: ConferenceRow, today: Optional[date] = None) -> bool:
    """Whether ``row`` is waiting on a next edition inside the check window.

    True when no future edition is on record and the latest edition's anchor
    (:func:`edition_anchor`) is between ``CHECK_WINDOW_MIN_MONTHS`` and
    ``CHECK_WINDOW_MAX_MONTHS`` old. Ignores the re-check interval.
    """
    today = today or date.today()
    if _has_future_edition(row, today):
        return False
    anchor = edition_anchor(row)
    if anchor is None:
        return False
    window_open = _add_months(anchor, CHECK_WINDOW_MIN_MONTHS)
    window_close = _add_months(anchor, CHECK_WINDOW_MAX_MONTHS)
    return window_open <= today <= window_close


def in_recent_window(row: ConferenceRow, today: Optional[date] = None) -> bool:
    """Whether ``row``'s last edition is over but the check window has not opened.

    True when no future edition is on record and the latest edition's anchor
    (:func:`edition_anchor`) is less than ``CHECK_WINDOW_MIN_MONTHS`` old. Many
    series announce the next edition in these months, often right after the
    meeting, so the watch cadence page-checks them (tier ``recent``).
    """
    today = today or date.today()
    if _has_future_edition(row, today):
        return False
    anchor = edition_anchor(row)
    if anchor is None:
        return False
    return today < _add_months(anchor, CHECK_WINDOW_MIN_MONTHS)


def is_retired(row: ConferenceRow, today: Optional[date] = None) -> bool:
    """Whether ``row`` has aged past the check window with no new edition.

    True when no future edition is on record and the latest edition's anchor
    (:func:`edition_anchor`) is more than ``CHECK_WINDOW_MAX_MONTHS`` old -- the
    point at which :func:`is_due_for_check` stops checking the series. Such a
    series is presumed discontinued (or renamed, or moved somewhere the agent
    has not found) and is left out of the published site, but its row is kept
    so it reappears if a later discovery run records a new edition. A row with
    no dates at all is never retired: it is a fresh seed awaiting its first fill.
    """
    today = today or date.today()
    if _has_future_edition(row, today):
        return False
    anchor = edition_anchor(row)
    if anchor is None:
        return False
    return today > _add_months(anchor, CHECK_WINDOW_MAX_MONTHS)


def is_due_for_check(row: ConferenceRow, today: Optional[date] = None) -> bool:
    """Whether ``row`` is due for an auto-check under the policy.

    See the module docstring for the full rule. ``today`` defaults to the
    current date; it is a parameter so the policy can be tested deterministically.
    """
    today = today or date.today()
    if _has_future_edition(row, today):
        # Already updated: nothing new to find until that edition passes.
        return False
    if edition_anchor(row) is None:
        # No date to anchor on. Check once if we never have (initial seed fill);
        # otherwise there is nothing to schedule against.
        return row.last_checked is None
    if not in_stale_window(row, today):
        return False  # too soon to expect new dates, or past the cutoff
    # Inside the window: honor the re-check interval since the last check.
    if row.last_checked is None:
        return True
    return (today - row.last_checked).days >= RECHECK_INTERVAL_DAYS


def due_subcategories(
    db_url: str = DEFAULT_DATABASE_URL, today: Optional[date] = None
) -> List[str]:
    """Distinct subcategories containing at least one due conference, sorted.

    Discovery runs per field, so this is the unit the scheduled refresh acts on.
    """
    today = today or date.today()
    engine = get_engine(db_url)
    with Session(engine) as session:
        rows = session.scalars(select(ConferenceRow))
        subs: set[str] = set()
        for row in rows:
            if is_due_for_check(row, today):
                # A row may carry several tags; each is a field worth refreshing.
                subs.update(normalize_subcategories(row.subcategory))
    return sorted(subs)


def due_conference_ids(
    db_url: str = DEFAULT_DATABASE_URL, today: Optional[date] = None
) -> List[str]:
    """Ids of the conferences currently due for a check (for reporting/targeting)."""
    today = today or date.today()
    engine = get_engine(db_url)
    with Session(engine) as session:
        rows = session.scalars(select(ConferenceRow))
        return sorted(row.id for row in rows if is_due_for_check(row, today))


def mark_subcategories_checked(
    subcategories: List[str],
    db_url: str = DEFAULT_DATABASE_URL,
    today: Optional[date] = None,
) -> int:
    """Stamp ``last_checked = today`` on every row in ``subcategories``.

    A discovery run covers a whole field, so after refreshing a subcategory every
    row in it has just been checked. Recording that on all of them (not only the
    ones that triggered the run) prevents redundant re-runs before the next
    interval elapses. Returns the number of rows stamped.
    """
    today = today or date.today()
    subs = {s for s in subcategories}
    if not subs:
        return 0
    engine = get_engine(db_url)
    with Session(engine) as session:
        # Substring match per tag: a row whose subcategory column lists several
        # tags (e.g. "radiology, pediatrics") was covered if any refreshed field
        # appears in it, so an exact ``IN`` match would miss multi-tag rows.
        conds = [ConferenceRow.subcategory.ilike(f"%{s}%") for s in subs]
        rows = list(
            session.scalars(select(ConferenceRow).where(or_(*conds)))
        )
        for row in rows:
            row.last_checked = today
        session.commit()
        return len(rows)


# --- Watch cadence: tiers, page checks, targeted re-research ------------------

# Tier order doubles as priority when the per-run agent cap defers some series.
WATCH_TIERS = ("daily", "soon", "stale", "recent", "initial")


def watch_tier(row: ConferenceRow, today: Optional[date] = None) -> Optional[str]:
    """The watch tier ``row`` falls in today, or ``None`` if it is not watched.

    - ``daily``: an upcoming submission deadline within
      ``WATCH_DAILY_WINDOW_DAYS`` before or after today.
    - ``soon``: an upcoming deadline or the meeting's start date within the next
      ``WATCH_SOON_WINDOW_DAYS`` days.
    - ``stale``: waiting on a next edition inside the check window
      (:func:`in_stale_window`).
    - ``recent``: the last edition is over, but the check window has not opened
      yet (:func:`in_recent_window`).
    - ``initial``: never researched and carrying no dates at all.
    """
    today = today or date.today()
    deadlines = [d for f in _UPCOMING_DEADLINE_FIELDS if (d := getattr(row, f)) is not None]
    if any(abs((d - today).days) <= WATCH_DAILY_WINDOW_DAYS for d in deadlines):
        return "daily"
    soon = deadlines + ([row.upcoming_start_date] if row.upcoming_start_date else [])
    if any(0 <= (d - today).days <= WATCH_SOON_WINDOW_DAYS for d in soon):
        return "soon"
    if in_stale_window(row, today):
        return "stale"
    if in_recent_window(row, today):
        return "recent"
    if edition_anchor(row) is None and row.last_checked is None:
        return "initial"
    return None


@dataclass
class WatchDecision:
    """What the watch run decided for one series."""

    id: str
    tier: str
    check: Optional[PageCheck]  # None when the row has no link to check
    status: str  # "changed" | "unchanged" | "baseline" | "unreadable"
    run_agent: bool
    reason: str

    @property
    def fingerprint(self) -> Optional[str]:
        return self.check.fingerprint if self.check else None


def _decide(row: ConferenceRow, tier: str, check: Optional[PageCheck], today: date) -> WatchDecision:
    """Apply the gate: does this series' page check warrant an agent run?"""
    age = (today - row.last_checked).days if row.last_checked else None
    interval_elapsed = age is None or age >= RECHECK_INTERVAL_DAYS
    since = "never researched" if age is None else f"researched {age}d ago"
    fp = check.fingerprint if check else None

    if fp is None:
        why = check.error if check else "no link"
        return WatchDecision(row.id, tier, check, "unreadable", interval_elapsed, f"{why}; {since}")
    if row.watch_fingerprint is None or row.watch_url != row.url:
        why = "first page check" if row.watch_fingerprint is None else "link changed"
        return WatchDecision(row.id, tier, check, "baseline", interval_elapsed, f"{why}; {since}")
    if fp != row.watch_fingerprint:
        return WatchDecision(row.id, tier, check, "changed", True, "dates on page changed")
    backstop = age is None or age >= WATCH_BACKSTOP_DAYS
    return WatchDecision(row.id, tier, check, "unchanged", backstop, since)


def _check_default(url: str) -> PageCheck:
    from conference_agent.page_watch import check_url

    return check_url(url)


def plan_watch(
    db_url: str = DEFAULT_DATABASE_URL,
    today: Optional[date] = None,
    check: Callable[[str], PageCheck] = _check_default,
    workers: int = 8,
) -> List[WatchDecision]:
    """Page-check every series whose tier makes it due today; return decisions.

    ``daily`` series are checked every run; the others once per
    ``RECHECK_INTERVAL_DAYS`` (by ``watch_checked``). Pure with respect to the
    database: nothing is written until :func:`commit_watch`. Each distinct URL is
    fetched once per run (several CSHL meetings share one listing page).
    Decisions are ordered by tier priority, then by id.
    """
    today = today or date.today()
    engine = get_engine(db_url)
    with Session(engine) as session:
        rows = list(session.scalars(select(ConferenceRow)))
        session.expunge_all()

    todo = []
    for row in rows:
        tier = watch_tier(row, today)
        if tier is None:
            continue
        if (
            tier != "daily"
            and row.watch_checked is not None
            and (today - row.watch_checked).days < RECHECK_INTERVAL_DAYS
        ):
            continue
        todo.append((row, tier))

    urls = sorted({row.url for row, _ in todo if row.url})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results: Dict[str, PageCheck] = dict(zip(urls, pool.map(check, urls)))

    decisions = [
        _decide(row, tier, results.get(row.url) if row.url else None, today)
        for row, tier in todo
    ]
    decisions.sort(key=lambda d: (WATCH_TIERS.index(d.tier), d.id))
    return decisions


def commit_watch(
    decisions: Sequence[WatchDecision],
    researched: Sequence[str],
    db_url: str = DEFAULT_DATABASE_URL,
    today: Optional[date] = None,
) -> None:
    """Record the outcome of a watch run.

    A series whose decision called for an agent run that did not happen
    (deferred by the cap, or its batch failed) is left untouched, so the next
    run re-plans it with the same evidence. Every other checked series gets
    ``watch_checked = today`` and, when its pages were readable, its fingerprint
    stored as the new baseline (together with the link it came from);
    ``researched`` series also get ``last_checked = today``.
    """
    today = today or date.today()
    done = set(researched)
    engine = get_engine(db_url)
    with Session(engine) as session:
        for d in decisions:
            if d.run_agent and d.id not in done:
                continue
            row = session.get(ConferenceRow, d.id)
            if row is None:
                continue
            row.watch_checked = today
            if d.fingerprint is not None:
                row.watch_fingerprint = d.fingerprint
                row.watch_url = row.url
            if d.id in done:
                row.last_checked = today
        session.commit()


@dataclass
class WatchReport:
    """Summary of a watch run, for logging and the notification email."""

    decisions: List[WatchDecision]
    researched: List[str] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    refreshed: List[Conference] = field(default_factory=list)
    changes: Dict[str, List[str]] = field(default_factory=dict)


_DIFF_FIELDS = (
    "prior_abstract_deadline",
    "prior_late_abstract_deadline",
    "prior_paper_deadline",
    "prior_start_date",
    "prior_end_date",
    "upcoming_abstract_deadline",
    "upcoming_late_abstract_deadline",
    "upcoming_paper_deadline",
    "upcoming_start_date",
    "upcoming_end_date",
    "deadline_time",
    "location",
    "url",
)


def _snapshot(ids: Sequence[str], db_url: str) -> Dict[str, Conference]:
    engine = get_engine(db_url)
    with Session(engine) as session:
        return {
            row.id: _row_to_model(row)
            for i in ids
            if (row := session.get(ConferenceRow, i)) is not None
        }


def _diff(before: Conference, after: Conference) -> List[str]:
    out = []
    for f in _DIFF_FIELDS:
        old, new = getattr(before, f), getattr(after, f)
        if old != new:
            out.append(f"{f}: {old or '—'} -> {new or '—'}")
    return out


def run_watch(
    db_url: str = DEFAULT_DATABASE_URL,
    backend: Optional[str] = None,
    today: Optional[date] = None,
    check: Callable[[str], PageCheck] = _check_default,
    refresh: Optional[Callable[..., List[Conference]]] = None,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> WatchReport:
    """Run one watch cycle: plan, re-research what changed, record the outcome.

    Before planning and after recording, finished upcoming editions are moved
    into the prior slots (:func:`database.roll_past_editions`).

    Series flagged for research are taken in tier-priority order up to
    ``WATCH_MAX_AGENT_PER_RUN`` and re-researched ``WATCH_BATCH_SIZE`` at a time
    via ``refresh`` (default :func:`discover.refresh_conferences`); results are
    merged fill-only (:func:`database.apply_refreshed_conferences`). A failed
    batch is logged and skipped so the rest of the run proceeds; its series are
    retried next run. ``dry_run`` plans and logs without researching or writing.
    """
    today = today or date.today()
    if not dry_run:
        _roll(db_url, today, log)
    decisions = plan_watch(db_url, today, check)
    report = WatchReport(decisions=decisions)

    for d in decisions:
        c = d.check
        detail = f"{c.dates} dates / {c.pages} page(s)" if c and c.fingerprint else ""
        action = "RESEARCH" if d.run_agent else "skip"
        log(f"  [{d.tier:7}] {d.id:18} {d.status:10} {action:8} {d.reason} {detail}".rstrip())

    wanted = [d.id for d in decisions if d.run_agent]
    selected, report.deferred = wanted[:WATCH_MAX_AGENT_PER_RUN], wanted[WATCH_MAX_AGENT_PER_RUN:]
    counts = {t: sum(d.tier == t for d in decisions) for t in WATCH_TIERS}
    log(
        f"Checked {len(decisions)} series ("
        + ", ".join(f"{n} {t}" for t, n in counts.items() if n)
        + f"); {len(wanted)} need research, {len(selected)} this run"
        + (f", {len(report.deferred)} deferred" if report.deferred else "")
        + "."
    )
    if dry_run:
        return report

    if refresh is None:
        from conference_agent.discover import DEFAULT_BACKEND, refresh_conferences

        backend = backend or DEFAULT_BACKEND

        def refresh(targets, attendance_hints=None):
            return refresh_conferences(
                targets, backend=backend, attendance_hints=attendance_hints
            )

    before = _snapshot(selected, db_url)
    hints = known_attendance_sources(db_url=db_url)
    for start in range(0, len(selected), WATCH_BATCH_SIZE):
        batch = [i for i in selected[start:start + WATCH_BATCH_SIZE] if i in before]
        if not batch:
            continue
        log(f"Researching: {', '.join(batch)}")
        try:
            found = refresh(
                [before[i] for i in batch],
                attendance_hints=attendance_hints_for([before[i] for i in batch], hints),
            )
        except Exception as exc:  # one failed batch must not sink the run
            log(f"  batch failed: {exc}")
            report.failed.extend(batch)
            continue
        apply_refreshed_conferences(found, db_url=db_url)
        report.refreshed.extend(found)
        report.researched.extend(batch)
        missing = sorted(set(batch) - {c.id for c in found})
        if missing:
            log(f"  no record returned for: {', '.join(missing)}")

    after = _snapshot(report.researched, db_url)
    for i in report.researched:
        if i in after and (lines := _diff(before[i], after[i])):
            report.changes[i] = lines
            log(f"  {i} updated:")
            for line in lines:
                log(f"    {line}")
    log(
        f"Researched {len(report.researched)} series; {len(report.changes)} changed"
        + (f"; {len(report.failed)} failed" if report.failed else "")
        + "."
    )

    commit_watch(decisions, report.researched, db_url, today)
    # Research can return an edition that has already ended as the upcoming one.
    _roll(db_url, today, log)
    return report


def _roll(db_url: str, today: date, log: Callable[[str], None]) -> None:
    """Run :func:`database.roll_past_editions` and log what it changed."""
    out = roll_past_editions(db_url, today)
    if out["duplicate"]:
        log(f"Merged duplicated prior/upcoming editions: {', '.join(out['duplicate'])}")
    if out["rolled"]:
        log(f"Moved finished editions to prior: {', '.join(out['rolled'])}")
