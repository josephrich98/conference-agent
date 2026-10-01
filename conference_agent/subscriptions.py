"""Per-conference update emails for site visitors who subscribed.

Visitors subscribe from the live site's ✉️ button. The Vercel Functions in
``web/vercel/api/`` confirm the address and store each (email, conference id)
pair in a private Vercel Blob store. This module is the sending half, run by the
local refresh job (``scripts/notify_subscribers.py``) after each refresh:

1. Compare each series' subscriber-facing fields (:data:`WATCHED_FIELDS`)
   against the snapshot saved on the previous run (``data/notify_state.json``).
2. Fetch the subscription list from the site's ``/api/subscriptions`` endpoint
   (bearer ``SUBSCRIBE_SECRET``).
3. Email every subscriber of a changed series a summary of what changed, with
   the series' updated ``.ics`` attached (same UIDs as the site's calendar
   button, so importing it updates the existing events in place), and signed
   unsubscribe links.

Separately, :func:`send_reminders` emails each subscriber once when one of the
series' upcoming submission deadlines is ``SUBSCRIBER_REMINDER_DAYS`` days away
or closer (state in ``data/reminder_state.json``).

The snapshot entry for a series only advances once its emails are sent (or it
has no subscribers), so a failed send is retried on the next run. The first run
has no snapshot and only records one.

Ids are slugs of conference names, so a series' id changes when it is renamed
(and every id changed once, when rows moved off acronym ids). Stored
subscriptions and snapshot entries keep the id they were written under; the
``resolve`` hook (``database.resolve_ids``) maps each to the current id, and an
unsubscribe link names the id the subscription is actually stored under.

Tokens are HMAC-SHA256 (hex) keyed by ``SUBSCRIBE_SECRET`` over the
newline-joined parts, identical to ``sign`` in ``web/vercel/api/_lib.js``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

from conference_agent.calendar_sync import conferences_to_ics
from conference_agent.database import NEW_EDITION_GAP_DAYS
from conference_agent.models import Conference

# (attribute, label) pairs whose change triggers an update email. Prior-edition
# fields are left out: they only change when an edition rolls over, which also
# changes the upcoming fields below.
WATCHED_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("name", "Name"),
    ("upcoming_abstract_deadline", "Abstract deadline"),
    ("upcoming_late_abstract_deadline", "Late abstract deadline"),
    ("upcoming_paper_deadline", "Paper deadline"),
    ("deadline_time", "Deadline time"),
    ("upcoming_start_date", "Conference start"),
    ("upcoming_end_date", "Conference end"),
    ("upcoming_registration", "Registration"),
    ("location", "Location"),
    ("remote_option", "Remote option"),
    ("cost", "Cost"),
    ("url", "Website"),
)

Change = Tuple[str, Optional[str], Optional[str]]  # (label, old, new)

_ATTR_FOR_LABEL = {label: attr for attr, label in WATCHED_FIELDS}


def sign(secret: str, *parts: str) -> str:
    """HMAC-SHA256 hex over the newline-joined parts (mirrors ``_lib.js``)."""
    return hmac.new(secret.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def unsubscribe_url(site_url: str, secret: str, email: str, conference_id: str) -> str:
    """Signed unsubscribe link; ``conference_id="*"`` unsubscribes from everything."""
    query = urlencode(
        {"email": email, "id": conference_id, "sig": sign(secret, "unsubscribe", email, conference_id)}
    )
    return f"{site_url.rstrip('/')}/api/unsubscribe?{query}"


def watched_snapshot(conf: Conference) -> Dict[str, Optional[str]]:
    """The subscriber-facing fields of a series, as JSON-friendly strings."""
    out: Dict[str, Optional[str]] = {}
    for attr, _ in WATCHED_FIELDS:
        value = getattr(conf, attr)
        if hasattr(value, "value"):  # enums
            value = value.value
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        out[attr] = value if value != "" else None
    return out


def diff_snapshots(old: Dict[str, Optional[str]], new: Dict[str, Optional[str]]) -> List[Change]:
    """Labeled (old, new) pairs for each watched field that differs."""
    return [
        (label, old.get(attr), new.get(attr))
        for attr, label in WATCHED_FIELDS
        if old.get(attr) != new.get(attr)
    ]


def load_state(path: Path) -> Optional[Dict[str, dict]]:
    """The previous run's snapshot, or ``None`` when there is none yet."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_state(path: Path, state: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def fetch_subscriptions(site_url: str, secret: str, timeout: float = 30) -> Dict[str, List[str]]:
    """Subscriber addresses by conference id, from the site's API."""
    if not site_url.startswith(("https://", "http://")):
        raise ValueError(f"site_url must be an http(s) URL, got {site_url!r}")
    req = urllib.request.Request(
        f"{site_url.rstrip('/')}/api/subscriptions",
        headers={"Authorization": f"Bearer {secret}", "User-Agent": "conference-agent"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme checked above)  # nosec B310
        payload = json.load(resp)
    by_id: Dict[str, List[str]] = {}
    for sub in payload.get("subscriptions", []):
        by_id.setdefault(sub["id"], []).append(sub["email"])
    return by_id


def drop_finished_edition(changes: List[Change], conf: Conference) -> List[Change]:
    """*changes* without the blanks left by moving a finished edition to prior.

    ``database.roll_past_editions`` clears the upcoming slots once a meeting is
    over and keeps the same dates in the prior slots, which the site shows in
    their place. An upcoming field that went from a date to blank while the
    prior slot now holds that date is that move, not news.
    """
    return [
        (label, old, new)
        for label, old, new in changes
        if not (
            new is None
            and old is not None
            and (attr := _ATTR_FOR_LABEL[label]).startswith("upcoming_")
            and _as_text(getattr(conf, "prior_" + attr[len("upcoming_"):], None)) == old
        )
    ]


def _as_text(value) -> Optional[str]:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value if value != "" else None


def new_edition_year(changes: List[Change], prior_start: Optional[date] = None) -> Optional[int]:
    """The new edition's year when *changes* record an edition rollover, else ``None``.

    A rollover is a conference start that moves forward by more than
    ``database.NEW_EDITION_GAP_DAYS`` -- the same rule ``_roll_editions`` uses to
    shift the stored upcoming edition into the prior slots. When the previous
    edition had already been moved to prior (so the old start is blank),
    ``prior_start`` is compared instead.
    """
    for label, old, new in changes:
        if _ATTR_FOR_LABEL.get(label) == "upcoming_start_date" and new:
            old_d = date.fromisoformat(old) if old else prior_start
            new_d = date.fromisoformat(new)
            if old_d and (new_d - old_d).days > NEW_EDITION_GAP_DAYS:
                return new_d.year
    return None


def _describe_change(change: Change, rollover: bool) -> str:
    label, old, new = change
    if old is None:
        return f"- {label}: {new} (newly announced)"
    if new is None and rollover and _ATTR_FOR_LABEL[label].startswith("upcoming_"):
        return f"- {label}: not yet announced (previous edition: {old})"
    return f"- {label}: {old or '—'} → {new or '—'}"


def build_update_email(
    conf: Conference,
    changes: List[Change],
    to_address: str,
    from_address: str,
    site_url: str,
    secret: str,
    subscription_id: Optional[str] = None,
) -> EmailMessage:
    """The update email for one subscriber: changes, the new schedule, the .ics.

    An edition rollover (see :func:`new_edition_year`) is announced as such, and
    the previous edition's dates that the new edition has not published yet are
    reported as "not yet announced" rather than as a change to a blank value.
    ``subscription_id`` is the id the subscription is stored under, when it
    predates the series' current id; the unsubscribe link must name it.
    """
    label = conf.acronym or conf.name
    one = unsubscribe_url(site_url, secret, to_address, subscription_id or conf.id)
    everything = unsubscribe_url(site_url, secret, to_address, "*")
    year = new_edition_year(changes, conf.prior_start_date)
    if year:
        subject = f"{label} {year} dates announced"
        intro = f"The {year} edition of {conf.name} ({label}) has been announced on Conference Agent."
    else:
        subject = f"{label} updated: " + ", ".join(lbl.lower() for lbl, _, _ in changes)
        intro = f"{conf.name} ({label}) was updated on Conference Agent."
    lines = [intro, "", "What changed:"]
    lines += [_describe_change(c, rollover=bool(year)) for c in changes]
    if year and any(new is None and _ATTR_FOR_LABEL[lbl].startswith("upcoming_") for lbl, _, new in changes):
        lines += [
            "",
            f"Some {year} details have not been published yet. You will get another",
            "email when they are.",
        ]
    lines += ["", "Current schedule:"]
    now = watched_snapshot(conf)
    lines += [f"- {lbl}: {now[attr]}" for attr, lbl in WATCHED_FIELDS if now[attr]]
    lines += [
        "",
        f"The attached {conf.id}.ics holds the updated deadlines and dates. Importing it",
        "updates the events from an earlier import instead of adding duplicates.",
        "",
        f"Browse all conferences: {site_url}",
        "",
        f"Stop emails about {label}: {one}",
        f"Stop all Conference Agent emails: {everything}",
    ]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Conference Agent <{from_address}>"
    msg["To"] = to_address
    msg["List-Unsubscribe"] = f"<{one}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content("\n".join(lines) + "\n")
    msg.add_attachment(
        conferences_to_ics([conf], calendar_name=label).encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename=f"{conf.id}.ics",
    )
    return msg


def subscribers_by_id(
    fetch: Callable[[], Dict[str, List[str]]],
    resolve: Optional[Callable[[Iterable[str]], Dict[str, str]]] = None,
) -> Dict[str, Dict[str, str]]:
    """Current id -> {email: the id that subscription is stored under}.

    ``fetch`` returns addresses by stored id; ``resolve`` maps stored ids
    (current or former) to current ones, omitting unknown ids. Without it, ids
    are taken as current.
    """
    stored = fetch()
    moved = resolve(list(stored)) if resolve is not None else {k: k for k in stored}
    subscribers: Dict[str, Dict[str, str]] = {}
    for stored_id, emails in stored.items():
        cid = moved.get(stored_id)
        for email in emails:
            if cid is not None:
                subscribers.setdefault(cid, {}).setdefault(email, stored_id)
    return subscribers


@dataclass
class NotifyReport:
    changed: Dict[str, List[Change]] = field(default_factory=dict)
    sent: List[Tuple[str, str]] = field(default_factory=list)  # (id, email)
    failed: List[Tuple[str, str]] = field(default_factory=list)
    initialized: bool = False


def notify_subscribers(
    conferences: Iterable[Conference],
    state_path: Path,
    site_url: str,
    secret: str,
    from_address: str,
    send: Callable[[EmailMessage], None],
    fetch: Callable[[], Dict[str, List[str]]],
    log: Callable[[str], None] = print,
    resolve: Optional[Callable[[Iterable[str]], Dict[str, str]]] = None,
) -> NotifyReport:
    """Email subscribers of every series whose watched fields changed.

    ``fetch`` returns subscriber addresses by conference id and ``send`` delivers
    one message; both are injected so the flow is testable offline. ``resolve``
    maps stored ids (current or former) to current ones, omitting unknown ids;
    without it, ids are taken as current.
    """
    report = NotifyReport()
    current = {c.id: (c, watched_snapshot(c)) for c in conferences}
    state = load_state(state_path)
    if state is not None and resolve is not None:
        # Carry entries recorded under a former id over to the current one; an
        # entry already under the current id wins.
        moved = resolve(list(state))
        remapped: Dict[str, dict] = {}
        for old_id, snap in state.items():
            remapped.setdefault(moved.get(old_id, old_id), snap)
        for cid in current:
            if cid in state:
                remapped[cid] = state[cid]
        state = remapped
    if state is None:
        save_state(state_path, {cid: snap for cid, (_, snap) in current.items()})
        report.initialized = True
        log(f"No previous snapshot; recorded {len(current)} series. Nothing sent.")
        return report

    report.changed = {
        cid: changes
        for cid, (conf, snap) in current.items()
        if cid in state
        and (changes := drop_finished_edition(diff_snapshots(state[cid], snap), conf))
    }
    subscribers = subscribers_by_id(fetch, resolve) if report.changed else {}

    for cid, (conf, snap) in current.items():
        if cid not in report.changed:
            state[cid] = snap  # new series, or unchanged
            continue
        ok = True
        for email, stored_id in sorted(subscribers.get(cid, {}).items()):
            try:
                send(
                    build_update_email(
                        conf, report.changed[cid], email, from_address, site_url, secret,
                        subscription_id=stored_id,
                    )
                )
                report.sent.append((cid, email))
            except Exception as exc:  # keep going; this series retries next run
                ok = False
                report.failed.append((cid, email))
                log(f"  send failed for {cid} -> {email}: {exc}")
        if ok:
            state[cid] = snap
    # Series no longer in the catalog drop out of the snapshot.
    save_state(state_path, {cid: snap for cid, snap in state.items() if cid in current})

    log(
        f"{len(report.changed)} series changed; sent {len(report.sent)} email(s)"
        + (f", {len(report.failed)} failed" if report.failed else "")
        + "."
    )
    return report


# --- Deadline reminders --------------------------------------------------------

# (kind, label) for the deadlines a reminder is sent for. The kind matches the
# ``upcoming_<kind>_deadline`` attribute and, with "_" -> "-", the calendar kind.
REMINDER_KINDS: Tuple[Tuple[str, str], ...] = (
    ("abstract", "Abstract deadline"),
    ("late_abstract", "Late abstract deadline"),
    ("paper", "Paper deadline"),
)


def due_reminders(
    conferences: Iterable[Conference], today: date, lead_days: int
) -> List[Tuple[Conference, str, date]]:
    """``(conference, kind, deadline)`` for each upcoming deadline within ``lead_days``.

    A deadline is due from ``lead_days`` before it through its own day, so a run
    that was missed (or a deadline added late) still produces one reminder.
    """
    due = []
    for conf in conferences:
        for kind, _ in REMINDER_KINDS:
            deadline = getattr(conf, f"upcoming_{kind}_deadline")
            if deadline is not None and 0 <= (deadline - today).days <= lead_days:
                due.append((conf, kind, deadline))
    return due


def _when(deadline: date, today: date) -> str:
    days = (deadline - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def build_reminder_email(
    conf: Conference,
    kind: str,
    deadline: date,
    today: date,
    to_address: str,
    from_address: str,
    site_url: str,
    secret: str,
    subscription_id: Optional[str] = None,
) -> EmailMessage:
    """The reminder for one subscriber that a deadline of ``conf`` is close."""
    from conference_agent.calendar_sync import deadline_time_for

    label = conf.acronym or conf.name
    what = dict(REMINDER_KINDS)[kind]
    when = _when(deadline, today)
    one = unsubscribe_url(site_url, secret, to_address, subscription_id or conf.id)
    everything = unsubscribe_url(site_url, secret, to_address, "*")
    day = f"{deadline:%A, %B} {deadline.day}, {deadline.year}"
    lines = [f"The {what.lower()} for {conf.name} ({label}) is {when}: {day}."]
    time_text = deadline_time_for(conf, kind.replace("_", "-"))
    if time_text:
        lines.append(f"Deadline time: {time_text}")
    if conf.url:
        lines += ["", f"Conference website: {conf.url}"]
    lines += [
        "",
        "Confirm the deadline on the official site; it can change.",
        "",
        f"Browse all conferences: {site_url}",
        "",
        f"Stop emails about {label}: {one}",
        f"Stop all Conference Agent emails: {everything}",
    ]

    msg = EmailMessage()
    msg["Subject"] = f"Reminder: {label} {what.lower()} {when} ({deadline:%b} {deadline.day})"
    msg["From"] = f"Conference Agent <{from_address}>"
    msg["To"] = to_address
    msg["List-Unsubscribe"] = f"<{one}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content("\n".join(lines) + "\n")
    return msg


@dataclass
class ReminderReport:
    due: List[Tuple[str, str, date]] = field(default_factory=list)  # (id, kind, deadline)
    sent: List[Tuple[str, str, str]] = field(default_factory=list)  # (id, kind, email)
    failed: List[Tuple[str, str, str]] = field(default_factory=list)


def send_reminders(
    conferences: Iterable[Conference],
    state_path: Path,
    site_url: str,
    secret: str,
    from_address: str,
    send: Callable[[EmailMessage], None],
    subscribers: Callable[[], Dict[str, Dict[str, str]]],
    lead_days: int,
    today: Optional[date] = None,
    log: Callable[[str], None] = print,
) -> ReminderReport:
    """Email each subscriber once when a subscribed series' deadline is near.

    ``subscribers`` returns current id -> {email: stored id} (see
    :func:`subscribers_by_id`); it is called only when a reminder is due. The
    state file records, per ``<id>|<kind>|<deadline>``, the addresses already
    reminded, so each subscriber gets one reminder per deadline, a failed send is
    retried on the next run, and a moved deadline is reminded again. Entries for
    past deadlines are pruned.
    """
    today = today or date.today()
    report = ReminderReport()
    state: Dict[str, List[str]] = load_state(state_path) or {}
    due = due_reminders(conferences, today, lead_days)
    report.due = [(c.id, kind, d) for c, kind, d in due]
    subs = subscribers() if due else {}
    for conf, kind, deadline in due:
        key = f"{conf.id}|{kind}|{deadline.isoformat()}"
        done = set(state.get(key, []))
        for email, stored_id in sorted(subs.get(conf.id, {}).items()):
            if email in done:
                continue
            try:
                send(
                    build_reminder_email(
                        conf, kind, deadline, today, email, from_address, site_url, secret,
                        subscription_id=stored_id,
                    )
                )
                done.add(email)
                report.sent.append((conf.id, kind, email))
            except Exception as exc:  # keep going; retried next run
                report.failed.append((conf.id, kind, email))
                log(f"  reminder failed for {conf.id} {kind} -> {email}: {exc}")
        if done:
            state[key] = sorted(done)
    save_state(
        state_path,
        {k: v for k, v in state.items() if date.fromisoformat(k.rsplit("|", 1)[1]) >= today},
    )
    log(
        f"{len(due)} deadline(s) within {lead_days} days; sent {len(report.sent)} reminder(s)"
        + (f", {len(report.failed)} failed" if report.failed else "")
        + "."
    )
    return report
