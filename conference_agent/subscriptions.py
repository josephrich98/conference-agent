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

The snapshot entry for a series only advances once its emails are sent (or it
has no subscribers), so a failed send is retried on the next run. The first run
has no snapshot and only records one.

Tokens are HMAC-SHA256 (hex) keyed by ``SUBSCRIBE_SECRET`` over the
newline-joined parts, identical to ``sign`` in ``web/vercel/api/_lib.js``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

from conference_agent.calendar_sync import conferences_to_ics
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
        out[attr] = value if value not in ("", "unknown") else None
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


def build_update_email(
    conf: Conference,
    changes: List[Change],
    to_address: str,
    from_address: str,
    site_url: str,
    secret: str,
) -> EmailMessage:
    """The update email for one subscriber: changes, the new schedule, the .ics."""
    label = conf.acronym or conf.name
    one = unsubscribe_url(site_url, secret, to_address, conf.id)
    everything = unsubscribe_url(site_url, secret, to_address, "*")
    lines = [f"{conf.name} ({label}) was updated on Conference Agent.", "", "What changed:"]
    lines += [f"- {lbl}: {old or '—'} → {new or '—'}" for lbl, old, new in changes]
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
    msg["Subject"] = f"{label} updated: " + ", ".join(lbl.lower() for lbl, _, _ in changes)
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
) -> NotifyReport:
    """Email subscribers of every series whose watched fields changed.

    ``fetch`` returns subscriber addresses by conference id and ``send`` delivers
    one message; both are injected so the flow is testable offline.
    """
    report = NotifyReport()
    current = {c.id: (c, watched_snapshot(c)) for c in conferences}
    state = load_state(state_path)
    if state is None:
        save_state(state_path, {cid: snap for cid, (_, snap) in current.items()})
        report.initialized = True
        log(f"No previous snapshot; recorded {len(current)} series. Nothing sent.")
        return report

    report.changed = {
        cid: changes
        for cid, (_, snap) in current.items()
        if cid in state and (changes := diff_snapshots(state[cid], snap))
    }
    subscribers = fetch() if report.changed else {}

    for cid, (conf, snap) in current.items():
        if cid not in report.changed:
            state[cid] = snap  # new series, or unchanged
            continue
        ok = True
        for email in sorted(set(subscribers.get(cid, []))):
            try:
                send(build_update_email(conf, report.changed[cid], email, from_address, site_url, secret))
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
