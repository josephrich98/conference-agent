"""Deadlines from OpenReview for the series it hosts (no agent, no scraping).

Many machine-learning meetings (ICLR, NeurIPS, ICML, CVPR, MIDL, ...) run their
submissions on OpenReview, whose public API (``api2.openreview.net``, readable
without an account) gives each edition's deadlines as exact UTC instants. For a
linked series this is cheaper than the page check plus an agent run, and more
precise: the Submission invitation's due date is what actually closes
submissions, so an extension shows up there as soon as it is made.

Per edition (venue id such as ``ICLR.cc/2026/Conference``) two requests are made:

- the venue **group**: ``start_date`` (text such as "Apr 23 2026", or epoch
  milliseconds), ``location``, ``website``, ``commitments_venue`` (an ACL-style
  venue whose papers are reviewed elsewhere; never linked), and ``date``, a
  summary such as "Submission Start: ..., Abstract Registration: Sep 20 2025
  11:59AM UTC-0, Submission Deadline: Sep 25 2025 11:59AM UTC-0" (often empty
  until the call is configured);
- the **Submission invitation**: its ``duedate`` is the abstract-registration
  deadline in a two-stage venue, else the only deadline.

The summary names both deadlines; without it the invitation's due date is
filed as the abstract deadline when the series' latest edition has a separate
abstract deadline, else as the paper deadline. OpenReview gives no end date,
cost, registration, or late-abstract deadline, and its start date can be a
workshop day, so a start date or location is only filled where the edition has
none. Each instant is stored in the series' own time zone when it falls at
23:59 there (or in AoE, the usual "23:59 AoE"), else in UTC.

**Linking.** :func:`link_series` matches unlinked rows against the public venue
list (one request): a venue id ending in ``/<year>/Conference`` whose path
names the series' acronym ("ICLR.cc", "thecvf.com/CVPR"), confirmed by its
website's domain matching the stored link (or, for an acronym no other series
shares, by the venue's short name). The link is stored as a template
(``openreview_venue``, e.g. ``ICLR.cc/{year}/Conference``); a rejected match
stores ``none`` so it is not retried. ``discover --openreview-link`` sets or
clears it by hand.

:func:`sync` runs both steps and reports which series it fully covered (the
upcoming edition's dates are all known); ``refresh.run_watch`` skips the page
check and agent run for those. Uses only the standard library.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import (
    NEW_EDITION_GAP_DAYS,
    ConferenceRow,
    apply_edition_fields,
    get_engine,
)
from conference_agent.deadline_time import TIMEZONES

API_URL = "https://api2.openreview.net"
USER_AGENT = "conference-agent/1.0 (+https://conferenceagent.vercel.app)"
TIMEOUT_SECONDS = 20
# Seconds between requests. OpenReview rate-limits clients (a 2026 response
# reported a limit of 100 requests); this keeps a run well under it.
REQUEST_INTERVAL = 0.7
RETRY_WAIT_SECONDS = 10
# Editions older than this many years before today are not looked at.
YEARS_BACK = 1
NO_LINK = "none"
# An OpenReview close time this close to the stored deadline instant is the same
# deadline (a grace period such as "23:59 AoE" closing at 01:00): kept as stored.
SAME_DEADLINE_MINUTES = 180
# Words that do not identify a series when comparing names with venue titles.
_NAME_STOPWORDS = frozenset(
    "the a an of on in for and to international annual conference meeting "
    "symposium workshop congress ieee acm cvf".split()
)

_YEAR_PART = re.compile(r"^(?:19|20)\d\d$")
_SUMMARY_ITEM = re.compile(
    r"(Submission Start|Abstract Registration|Submission Deadline)\s*:\s*"
    r"([A-Z][a-z]{2} \d{1,2} \d{4} \d{1,2}:\d{2}\s*[AP]M)\s*"
    r"(?:UTC|GMT)\s*([+\-−]\s*\d{1,2}(?::?\d{2})?)?",
)


class OpenReviewError(RuntimeError):
    """The API could not be reached or answered with an unexpected error."""


# --- HTTP client --------------------------------------------------------------


class OpenReviewClient:
    """A minimal, throttled, read-only client for the public OpenReview API."""

    def __init__(
        self,
        base_url: str = API_URL,
        interval: float = REQUEST_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if urllib.parse.urlparse(base_url).scheme != "https":
            raise ValueError(f"OpenReview base URL must be https: {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self._sleep = sleep
        self._last = 0.0
        self._venues: Optional[List[str]] = None
        self.requests = 0

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(3):
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:  # nosec B310 - https checked in __init__
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    self._sleep(RETRY_WAIT_SECONDS)
                    continue
                body = exc.read().decode("utf-8", "replace")
                if exc.code in (400, 403, 404):
                    # Not found / not visible to a guest: no data, not an outage.
                    return {"_error": body[:300]}
                raise OpenReviewError(f"{path}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                raise OpenReviewError(f"{path}: {exc}") from exc
        raise OpenReviewError(f"{path}: rate limited")

    def venue_ids(self) -> List[str]:
        """Every venue id OpenReview lists (the members of its ``venues`` group)."""
        if self._venues is None:
            groups = self._get("groups", {"id": "venues"}).get("groups") or []
            self._venues = list(groups[0].get("members") or []) if groups else []
        return self._venues

    def group(self, group_id: str) -> Optional[dict]:
        groups = self._get("groups", {"id": group_id}).get("groups") or []
        return groups[0] if groups else None

    def invitation(self, invitation_id: str) -> Optional[dict]:
        found = self._get(
            "invitations", {"id": invitation_id, "expired": "true"}
        ).get("invitations") or []
        return found[0] if found else None


# --- Parsing ------------------------------------------------------------------


def _content(group: dict, key: str):
    value = (group.get("content") or {}).get(key)
    return value.get("value") if isinstance(value, dict) else value


def _from_millis(value) -> Optional[datetime]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def parse_summary(text: Optional[str]) -> Dict[str, datetime]:
    """The deadlines in a venue's ``date`` summary, as aware UTC datetimes.

    Keys: ``start`` (submissions open), ``abstract`` (abstract registration),
    ``paper`` (submission deadline). Items that do not parse are left out.
    """
    out: Dict[str, datetime] = {}
    keys = {
        "Submission Start": "start",
        "Abstract Registration": "abstract",
        "Submission Deadline": "paper",
    }
    for label, when, offset in _SUMMARY_ITEM.findall(text or ""):
        try:
            text = re.sub(r"\s*([AP]M)$", r" \1", re.sub(r"\s+", " ", when))
            local = datetime.strptime(text, "%b %d %Y %I:%M %p")
        except ValueError:
            continue
        minutes = 0
        if offset:
            m = re.match(r"([+\-−])\s*(\d{1,2})(?::?(\d{2}))?", offset)
            sign = -1 if m.group(1) in "-−" else 1
            minutes = sign * (int(m.group(2)) * 60 + int(m.group(3) or 0))
        tz = timezone(timedelta(minutes=minutes))
        out[keys[label]] = local.replace(tzinfo=tz).astimezone(timezone.utc)
    return out


def parse_start_date(value) -> Optional[date]:
    """A venue's ``start_date``: text ("Apr 23 2026") or epoch milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Stored as midnight UTC of the day (sometimes local midnight a few
        # hours either side); noon-rounding keeps the intended calendar day.
        return (_from_millis(value) + timedelta(hours=12)).date()
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%b %d %Y", "%B %d %Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


@dataclass
class Edition:
    """One edition's facts as OpenReview publishes them."""

    venue_id: str
    year: int
    start_date: Optional[date] = None
    location: Optional[str] = None
    website: Optional[str] = None
    abstract: Optional[datetime] = None  # aware UTC
    paper: Optional[datetime] = None  # aware UTC
    # The Submission invitation's due date when the summary does not say which
    # deadline it is (filed by :func:`plan_update`).
    single: Optional[datetime] = None

    @property
    def deadlines(self) -> List[datetime]:
        return [d for d in (self.abstract, self.paper, self.single) if d is not None]


def edition_from_api(venue_id: str, year: int, group: dict, invitation: Optional[dict]) -> Edition:
    """Combine a venue group and its Submission invitation into an :class:`Edition`."""
    summary = parse_summary(_content(group, "date"))
    due = _from_millis(invitation.get("duedate")) if invitation else None
    ed = Edition(
        venue_id=venue_id,
        year=year,
        start_date=parse_start_date(_content(group, "start_date")),
        location=(_content(group, "location") or "").strip() or None,
        website=(_content(group, "website") or "").strip() or None,
    )
    if "abstract" in summary and summary["abstract"] == summary.get("paper"):
        ed.paper = due or summary["paper"]  # one deadline, listed twice
    elif "abstract" in summary:
        # Two stages. The invitation closes at abstract registration and is the
        # live value (it moves with an extension); the summary has the paper.
        ed.abstract = due or summary["abstract"]
        ed.paper = summary.get("paper")
    elif "paper" in summary:
        ed.paper = due or summary["paper"]
    else:
        ed.single = due
    return ed


# --- Venue ids ----------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def venue_templates(venue_ids: Iterable[str]) -> Dict[str, List[int]]:
    """Main-track venue templates and their years.

    ``thecvf.com/CVPR/2027/Conference`` -> ``{"thecvf.com/CVPR/{year}/Conference":
    [2027, ...]}``. Only ids ending in ``/<year>/Conference`` are kept
    (workshops, tracks, and journals are not series of the table).
    """
    out: Dict[str, List[int]] = {}
    for vid in venue_ids:
        parts = vid.split("/")
        if len(parts) < 3 or parts[-1] != "Conference" or not _YEAR_PART.match(parts[-2]):
            continue
        template = "/".join([*parts[:-2], "{year}", "Conference"])
        out.setdefault(template, []).append(int(parts[-2]))
    for years in out.values():
        years.sort(reverse=True)
    return out


def template_names(template: str) -> set:
    """The normalized names a template's path gives a venue ("ICLR.cc" -> iclr)."""
    names = set()
    for part in template.split("/"):
        if part in ("{year}", "Conference"):
            continue
        names.add(_norm(part))
        names.add(_norm(part.split(".")[0]))
    names.discard("")
    return names


def _name_words(text: str) -> set:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {w for w in words if w not in _NAME_STOPWORDS and len(w) > 1}


def title_matches(name: str, title: Optional[str]) -> bool:
    """Whether a venue title names the series: it contains every
    identifying word of the stored name (at least two of them)."""
    wanted = _name_words(name)
    return len(wanted) >= 2 and wanted <= _name_words(title or "")


def _domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    host = urllib.parse.urlparse(url if "//" in url else f"//{url}").hostname or ""
    labels = [p for p in host.lower().split(".") if p]
    return ".".join(labels[-2:]) if len(labels) >= 2 else None


# --- Fetching -----------------------------------------------------------------


def fetch_edition(
    client: OpenReviewClient,
    template: str,
    years: Iterable[int],
    today: date,
) -> Optional[Edition]:
    """The newest listed edition of ``template`` that has a deadline.

    Editions are tried newest first, from next year back to ``YEARS_BACK``
    years ago; a venue created before its call is configured has no deadline
    yet, so the one before it is used. ``None`` when none has one, or the venue
    is a commitments venue (reviewed elsewhere).
    """
    for year in sorted({y for y in years if y >= today.year - YEARS_BACK}, reverse=True):
        venue_id = template.replace("{year}", str(year))
        group = client.group(venue_id)
        if group is None:
            continue
        if _content(group, "commitments_venue") is True:
            return None
        invitation_id = _content(group, "submission_id") or f"{venue_id}/-/Submission"
        invitation = client.invitation(invitation_id)
        ed = edition_from_api(venue_id, year, group, invitation)
        if ed.deadlines:
            return ed
    return None


# --- Mapping onto a row -------------------------------------------------------


def _tzinfo(code: Optional[str]):
    """A tzinfo for a stored zone code, offset, or IANA name; None if unknown."""
    if not code:
        return None
    spec = TIMEZONES.get(code)
    if spec and "offset" in spec:
        return timezone(timedelta(minutes=spec["offset"]))
    name = spec["iana"] if spec else code
    m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def local_deadline(instant: datetime, zones: Iterable[Optional[str]]) -> tuple:
    """``(date, "HH:MM", zone)`` for a UTC deadline instant.

    The first of ``zones`` (the series' stored zones, then AoE) in which the
    instant is 23:59 is used, since that is how deadlines are announced;
    failing that, the first stored zone that resolves, else UTC.
    """
    candidates = [z for z in zones if z]
    for code in [*candidates, "AoE"]:
        tz = _tzinfo(code)
        if tz is not None:
            local = instant.astimezone(tz)
            if (local.hour, local.minute) == (23, 59):
                return local.date(), "23:59", code
    for code in [*candidates, "UTC"]:
        tz = _tzinfo(code)
        if tz is not None:
            local = instant.astimezone(tz)
            return local.date(), local.strftime("%H:%M"), code
    raise AssertionError("UTC always resolves")


def _same(a: Optional[date], b: Optional[date]) -> Optional[bool]:
    if a is None or b is None:
        return None
    return abs((a - b).days) <= NEW_EDITION_GAP_DAYS


@dataclass
class Plan:
    """What :func:`plan_update` decided for one row."""

    slot: Optional[str]  # "upcoming" | "prior" | None (edition not placeable)
    fields: dict = field(default_factory=dict)
    new_edition: bool = False
    reason: str = ""


def plan_update(row: ConferenceRow, ed: Edition, today: date) -> Plan:
    """Decide which edition slot ``ed`` is and the column values to write."""
    # File an unlabeled invitation deadline under the kind the series uses.
    abstract, paper = ed.abstract, ed.paper
    if ed.single is not None:
        has_up = any(
            getattr(row, f"upcoming_{k}_deadline") for k in ("abstract", "paper")
        )
        edition = "upcoming" if has_up else "prior"
        a = getattr(row, f"{edition}_abstract_deadline")
        p = getattr(row, f"{edition}_paper_deadline")
        if a is not None and (p is None or a < p):
            abstract = ed.single
        else:
            paper = ed.single

    zones = {
        kind: [getattr(row, f"{kind}_timezone"), row.abstract_timezone, row.paper_timezone]
        for kind in ("abstract", "paper")
    }
    local = {
        kind: local_deadline(instant, zones[kind])
        for kind, instant in (("abstract", abstract), ("paper", paper))
        if instant is not None
    }

    def same_edition(slot: str) -> Optional[bool]:
        verdict = _same(ed.start_date, getattr(row, f"{slot}_start_date"))
        if verdict is not None:
            return verdict
        for kind, (day, _, _) in local.items():
            verdict = _same(day, getattr(row, f"{slot}_{kind}_deadline"))
            if verdict is not None:
                return verdict
        return None

    up, prior = same_edition("upcoming"), same_edition("prior")
    up_dates = [
        getattr(row, f"upcoming_{s}")
        for s in ("abstract_deadline", "late_abstract_deadline", "paper_deadline",
                  "start_date", "end_date")
        if getattr(row, f"upcoming_{s}") is not None
    ]
    plan = Plan(slot=None)
    if up:
        plan.slot = "upcoming"
    elif prior:
        plan.slot = "prior"
    elif not _is_newer(row, ed, today):
        plan.reason = "older than the stored editions, or already over"
        return plan
    elif not up_dates:
        plan.slot = "upcoming"  # a newly announced edition, nothing to displace
    elif up is False and ed.deadlines and min(ed.deadlines).date() > max(up_dates):
        if any(d >= today for d in up_dates):
            plan.reason = "stored upcoming edition not over yet"
            return plan
        plan.slot, plan.new_edition = "upcoming", True
    else:
        plan.reason = "edition does not match a stored one"
        return plan

    slot = plan.slot
    for kind, (day, clock, zone) in local.items():
        instant = abstract if kind == "abstract" else paper
        stored = None if plan.new_edition else stored_instant(row, slot, kind)
        if stored is not None and abs((instant - stored).total_seconds()) <= SAME_DEADLINE_MINUTES * 60:
            continue  # the same deadline (or its grace period): keep it as stored
        plan.fields[f"{slot}_{kind}_deadline"] = day
        if slot == "upcoming" or not getattr(row, f"upcoming_{kind}_deadline"):
            plan.fields[f"{kind}_time"] = clock
            plan.fields[f"{kind}_timezone"] = zone
    # Start date and location only fill blanks: OpenReview's start date can be
    # a workshop day, and its location is usually less specific than ours.
    blank_start = plan.new_edition or getattr(row, f"{slot}_start_date") is None
    if ed.start_date and blank_start:
        plan.fields[f"{slot}_start_date"] = ed.start_date
    blank_location = plan.new_edition or not getattr(row, f"{slot}_location")
    if ed.location and blank_location:
        plan.fields[f"{slot}_location"] = ed.location
    return plan


def stored_instant(row: ConferenceRow, slot: str, kind: str) -> Optional[datetime]:
    """The UTC instant a stored deadline closes, by the series' time and zone.

    With no stored time the end of the day is used, and with no usable zone
    AoE (the latest day-end anywhere), as the table's coloring does.
    """
    day = getattr(row, f"{slot}_{kind}_deadline")
    if day is None:
        return None
    clock = getattr(row, f"{kind}_time") or "23:59"
    tz = _tzinfo(getattr(row, f"{kind}_timezone")) or _tzinfo("AoE")
    hour, minute = (int(x) for x in clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)


def _is_newer(row: ConferenceRow, ed: Edition, today: date) -> bool:
    """Whether ``ed`` can be a new upcoming edition of the row.

    Its meeting must not have started yet (with no start date, its last
    deadline must be less than a month past), and it must be later than the
    stored prior edition.
    """
    if ed.start_date is not None:
        if ed.start_date < today:
            return False
    elif not ed.deadlines or max(ed.deadlines).date() < today - timedelta(days=30):
        return False
    prior = row.prior_start_date
    if prior is not None and ed.start_date is not None:
        return ed.start_date > prior
    prior_deadlines = [
        getattr(row, f"prior_{k}_deadline") for k in ("abstract", "late_abstract", "paper")
    ]
    prior_deadlines = [d for d in prior_deadlines if d is not None]
    return not prior_deadlines or min(ed.deadlines).date() > max(prior_deadlines)


def is_covered(row: ConferenceRow, today: date) -> bool:
    """Whether the row's upcoming edition is complete enough to skip research.

    True when the meeting has a start date not yet past and every deadline
    kind the series uses (any dated, in either edition) has an upcoming date.
    """
    start = row.upcoming_start_date
    if start is None or (row.upcoming_end_date or start) < today:
        return False
    used = [
        k for k in ("abstract", "late_abstract", "paper")
        if getattr(row, f"upcoming_{k}_deadline") or getattr(row, f"prior_{k}_deadline")
    ]
    return all(getattr(row, f"upcoming_{k}_deadline") for k in used)


# --- Linking ------------------------------------------------------------------


def candidate_templates(row: ConferenceRow, templates: Dict[str, List[int]]) -> List[str]:
    """Templates whose path names the row's acronym."""
    acronym = _norm(row.acronym)
    if not acronym:
        return []
    return sorted(t for t in templates if acronym in template_names(t))


def link_series(
    client: OpenReviewClient,
    session: Session,
    today: date,
    log: Callable[[str], None] = print,
) -> List[str]:
    """Link unlinked rows to their OpenReview venue; returns the linked ids."""
    rows = list(session.scalars(select(ConferenceRow)))
    unlinked = [r for r in rows if r.openreview_venue is None]
    if not unlinked:
        return []
    templates = venue_templates(client.venue_ids())
    linked = []
    for row in unlinked:
        candidates = candidate_templates(row, templates)
        if not candidates:
            continue  # re-evaluated next run against the fresh venue list
        chosen, visible = None, False
        for template in candidates:
            years = [y for y in templates[template] if y >= today.year - YEARS_BACK] or templates[template][:1]
            group = client.group(template.replace("{year}", str(years[0])))
            if group is None or not group.get("content"):
                continue  # not visible to a guest (yet): try again next run
            visible = True
            if _content(group, "commitments_venue") is True:
                continue
            website = _content(group, "website")
            same_site = _domain(website) is not None and _domain(website) == _domain(row.url)
            if same_site or title_matches(row.name, _content(group, "title")):
                chosen = template
                break
        if chosen:
            row.openreview_venue = chosen
            linked.append(row.id)
            log(f"  linked {row.id} -> {chosen}")
        elif visible:
            row.openreview_venue = NO_LINK
            log(
                f"  {row.id}: not linked; {', '.join(candidates)} is another meeting "
                "(website and title differ) or a commitments venue"
            )
    return linked


# --- Sync ---------------------------------------------------------------------


@dataclass
class SyncReport:
    linked: List[str] = field(default_factory=list)
    updated: Dict[str, List[str]] = field(default_factory=dict)  # id -> changed columns
    covered: Dict[str, str] = field(default_factory=dict)  # id -> venue id
    skipped: Dict[str, str] = field(default_factory=dict)  # id -> reason
    failed: List[str] = field(default_factory=list)


def sync(
    db_url: str = DEFAULT_DATABASE_URL,
    today: Optional[date] = None,
    client: Optional[OpenReviewClient] = None,
    ids: Optional[Iterable[str]] = None,
    link: bool = True,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> SyncReport:
    """Link new series, then write each linked series' OpenReview deadlines.

    ``ids`` limits the series synced (linking still covers every unlinked
    row unless ``link`` is false). ``dry_run`` makes the same requests and
    reports what would change, writing nothing. A series whose requests fail
    is listed in ``failed`` and left as it was; a failure to reach the API at
    all ends the run with what was done so far.
    """
    today = today or date.today()
    client = client or OpenReviewClient()
    report = SyncReport()
    wanted = set(ids) if ids is not None else None
    engine = get_engine(db_url)
    with Session(engine) as session:
        try:
            if link:
                report.linked = link_series(client, session, today, log)
            linked_rows = [
                r for r in session.scalars(select(ConferenceRow))
                if r.openreview_venue and r.openreview_venue != NO_LINK
                and (wanted is None or r.id in wanted)
            ]
            templates = venue_templates(client.venue_ids()) if linked_rows else {}
        except OpenReviewError as exc:
            log(f"  OpenReview unavailable: {exc}")
            report.failed.append("*")
            if not dry_run:
                session.commit()
            return report
        for row in sorted(linked_rows, key=lambda r: r.id):
            template = row.openreview_venue
            years = templates.get(template) or [today.year + 1, today.year]
            try:
                ed = fetch_edition(client, template, years, today)
            except OpenReviewError as exc:
                log(f"  {row.id}: {exc}")
                report.failed.append(row.id)
                continue
            if ed is None:
                report.skipped[row.id] = "no edition with a deadline"
                continue
            plan = plan_update(row, ed, today)
            if plan.slot is None:
                report.skipped[row.id] = f"{ed.venue_id}: {plan.reason}"
                log(f"  {row.id}: {ed.venue_id} not applied ({plan.reason})")
                continue
            before = {f: getattr(row, f) for f in plan.fields}
            changed = apply_edition_fields(row, plan.fields, today, new_edition=plan.new_edition)
            if changed:
                report.updated[row.id] = changed
                detail = ", ".join(
                    f"{f}: {before.get(f) or '—'} -> {getattr(row, f) or '—'}"
                    for f in changed if f != "edition"
                )
                prefix = "new edition; " if plan.new_edition else ""
                log(f"  {row.id} ({ed.venue_id}): {prefix}{detail}")
            if plan.slot == "upcoming" and is_covered(row, today):
                report.covered[row.id] = ed.venue_id
        if dry_run:
            session.rollback()
        else:
            session.commit()
    return report


def set_link(row_id: str, venue: Optional[str], db_url: str = DEFAULT_DATABASE_URL) -> str:
    """Set a row's OpenReview link by hand.

    ``venue`` is a template (``ICLR.cc/{year}/Conference``), a venue id with a
    year (converted to a template), ``none`` (never link), or ``auto`` / None
    (clear, so the next sync tries to link it). Returns the stored value.
    """
    value: Optional[str]
    if venue is None or venue.strip().lower() == "auto":
        value = None
    elif venue.strip().lower() == NO_LINK:
        value = NO_LINK
    else:
        parts = venue.strip().strip("/").split("/")
        parts = ["{year}" if _YEAR_PART.match(p) else p for p in parts]
        if "{year}" not in parts:
            raise ValueError(f"{venue!r} has no year part (e.g. ICLR.cc/{{year}}/Conference)")
        value = "/".join(parts)
    with Session(get_engine(db_url)) as session:
        row = session.get(ConferenceRow, row_id)
        if row is None:
            raise ValueError(f"no conference with id {row_id}")
        row.openreview_venue = value
        session.commit()
    return value or "auto"
