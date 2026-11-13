"""Fill acceptance rates and decision dates from ccfddl, and cross-check deadlines.

ccfddl (https://ccfddl.com, MIT-licensed data at github.com/ccfddl/ccf-deadlines)
tracks computer-science conferences. It publishes two JSON files:

- ``conference/allacc.json``: per-year acceptance statistics (accepted and
  submitted counts, with each figure's source).
- ``conference/allconf.json``: each series' editions with their deadlines,
  including the acceptance decision (notification) date.

``--download`` saves both into ``data/ccfddl/`` (gitignored); without it the
saved files are used, so a run is reproducible offline.

A stored series is matched to a ccfddl series with the rule
``scripts/fill_rankings.py`` uses for the CCF / CORE lists: the same acronym
**and** a similar full name. For each match:

- **Acceptance rates** are written through ``database.merge_records``: a year
  ccfddl lists replaces the stored entry for that year, and other stored years
  are kept.
- **Decision dates** fill a blank ``prior_`` / ``upcoming_decision_deadline``
  of the edition whose start date falls in the same year as the ccfddl
  edition, through ``database.apply_edition_fields`` (pinned columns are left
  alone). A stored decision date is never overwritten.
- **Cross-check**: the abstract and paper deadlines of the same edition are
  compared, and disagreements are reported (never written), since either side
  may be the stale one.

Only the first timeline entry of an edition (the main submission round) is
used; later rounds are listed in ccfddl with a ``comment``.

Usage::

    python scripts/fill_ccfddl.py [--download] [--dry-run] [--db URL]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conference_agent.config import DEFAULT_DATABASE_URL  # noqa: E402
from scripts.fill_rankings import Entry, Match, match  # noqa: E402

CCFDDL_DIR = ROOT / "data" / "ccfddl"
CONF_FILE = CCFDDL_DIR / "allconf.json"
ACC_FILE = CCFDDL_DIR / "allacc.json"
BASE_URL = "https://ccfddl.com/conference/"
_USER_AGENT = "Mozilla/5.0 (compatible; conference-agent ccfddl fill)"

# Reviewed matches the automatic rule cannot see (ccfddl lists the venue under
# another acronym, a former name, or its SIG's name): stored acronym -> ccfddl
# series key. Applied only while ccfddl still carries that key. Same-acronym
# series in other fields (ATS, CNS, ISC, ISS are medical meetings here) are
# correctly left unmatched.
REVIEWED = {
    "KDD": "DB/sigkdd",
    "IEEE S&P": "SC/sp",
    "UbiComp": "HI/ubicomp",
    "EUROCRYPT": "SC/eurocrypt",
    "IEEE VIS": "CG/vis",
    "IEEE VR": "CG/vr",
    "Interspeech": "CG/interspeech",
    "NAACL": "AI/naacl",
    "SIGCOMM": "NW/sigcomm",
    "SIGMETRICS": "DS/sigmetrics",
    "WWW": "MX/www",
}

# ccfddl timeline key -> our deadline kind (compared, never written) and the
# decision date (filled when blank).
_COMPARED = {"abstract_deadline": "abstract", "deadline": "paper"}


def download() -> None:
    CCFDDL_DIR.mkdir(parents=True, exist_ok=True)
    for path in (CONF_FILE, ACC_FILE):
        req = urllib.request.Request(BASE_URL + path.name, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https URL)  # nosec B310
            data = resp.read()
        json.loads(data)  # refuse to save a non-JSON error page
        path.write_bytes(data)
        print(f"Saved {path.relative_to(ROOT)} ({len(data):,} bytes)")


def _zone(tz):
    """A ``tzinfo`` for a stored or ccfddl zone ("AoE", "UTC-12", "ET", ...),
    or None when it cannot be read."""
    from conference_agent.deadline_time import TIMEZONES, normalize_timezone

    code = normalize_timezone(tz)
    if code is None:
        return None
    spec = TIMEZONES.get(code)
    if spec and "offset" in spec:
        return timezone(timedelta(minutes=spec["offset"]))
    m = re.match(r"^UTC([+-])(\d{1,2})(?::(\d{2}))?$", code)
    if m:
        minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
        return timezone(timedelta(minutes=minutes if m.group(1) == "+" else -minutes))
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(spec["iana"] if spec else code)
    except Exception:
        return None


def ccfddl_instant(value, tz) -> datetime | None:
    """The instant of a ccfddl timestamp in its edition's zone, or None."""
    zone = _zone(tz)
    try:
        local = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return local.replace(tzinfo=zone) if zone else None


def same_deadline(ours: date, time: str | None, tz: str | None, theirs: datetime) -> bool:
    """Whether a stored deadline names the instant ccfddl gives.

    ccfddl often converts a deadline to another zone ("May 4 23:59 AoE" is
    listed as "May 5 11:59 UTC"), so dates alone disagree by a day. With a
    stored time and zone the two instants are compared (a minute apart counts
    as equal: 23:59 vs 23:59:59); otherwise ccfddl's instant is read as a date
    in the stored zone, or AoE when none is stored.
    """
    zone = _zone(tz) or _zone("AoE")
    if time:
        hours, minutes = (int(x) for x in time.split(":"))
        mine = datetime(ours.year, ours.month, ours.day, hours, minutes, tzinfo=zone)
        return abs((mine - theirs).total_seconds()) <= 60
    return theirs.astimezone(zone).date() == ours


def series_entries(conf_data: list) -> list[Entry]:
    """ccfddl series as ``fill_rankings.Entry`` (rank = the series key), so the
    same acronym + name matching applies."""
    return [Entry(s["title"], s.get("description") or "", s["conference_key"]) for s in conf_data]


def acceptance_for(key: str, acc_data: list) -> list[dict]:
    """ccfddl's acceptance entries for one series key, as merge-ready dicts."""
    for item in acc_data:
        if item.get("conference_key") == key:
            return [
                {
                    "year": r["year"], "accepted": r.get("accepted"),
                    "submitted": r.get("submitted"), "rate": r.get("rate"),
                    "source": r.get("source"),
                }
                for r in item.get("accept_rates") or []
                if r.get("year")
            ]
    return []


def edition_dates(edition: dict) -> dict:
    """The main round's timestamps of one ccfddl edition: ``{key: instant}``
    (an unreadable or 'TBD' value is left out)."""
    timeline = edition.get("timeline") or [{}]
    main = timeline[0]
    tz = edition.get("timezone")
    out = {k: ccfddl_instant(main.get(k), tz) for k in (*_COMPARED, "decision_deadline")}
    return {k: v for k, v in out.items() if v}


def plan_row(row, series: dict) -> tuple[dict, list[str]]:
    """The decision dates to fill on ``row`` and the deadline disagreements."""
    fills: dict = {}
    notes: list[str] = []
    by_year = {e.get("year"): e for e in series.get("confs") or []}
    for slot in ("upcoming", "prior"):
        start = getattr(row, f"{slot}_start_date")
        edition = by_year.get(start.year) if start else None
        if edition is None:
            continue
        found = edition_dates(edition)
        decision = found.get("decision_deadline")
        if decision and getattr(row, f"{slot}_decision_deadline") is None:
            # A decision date has no time of its own: its date where announced.
            fills[f"{slot}_decision_deadline"] = decision.date()
        for key, kind in _COMPARED.items():
            ours, theirs = getattr(row, f"{slot}_{kind}_deadline"), found.get(key)
            if theirs is None:
                continue
            shown = f"{theirs:%Y-%m-%d %H:%M} {edition.get('timezone') or ''}".strip()
            time, tz = getattr(row, f"{kind}_time"), getattr(row, f"{kind}_timezone")
            if ours is None:
                notes.append(f"{row.acronym} {start.year} {kind}: blank here, ccfddl {shown}")
            elif not same_deadline(ours, time, tz, theirs):
                stored = " ".join(str(p) for p in (ours, time, tz) if p)
                notes.append(f"{row.acronym} {start.year} {kind}: stored {stored}, ccfddl {shown}")
    return fills, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--download", action="store_true", help="Fetch ccfddl's data into data/ccfddl/ first")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--db", default=DEFAULT_DATABASE_URL, help="Database URL (default %(default)s)")
    args = parser.parse_args(argv)

    if args.download:
        download()
    if not (CONF_FILE.exists() and ACC_FILE.exists()):
        print("The ccfddl data is missing; run with --download.", file=sys.stderr)
        return 1
    conf_data = json.loads(CONF_FILE.read_text(encoding="utf-8"))
    acc_data = json.loads(ACC_FILE.read_text(encoding="utf-8"))
    entries = series_entries(conf_data)
    by_key = {s["conference_key"]: s for s in conf_data}

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from conference_agent.database import (
        ConferenceRow,
        apply_edition_fields,
        get_engine,
        merge_records,
    )
    from conference_agent.models import merge_acceptance_rates

    records, matched, review, decisions, disagreements = [], [], [], [], []
    with Session(get_engine(args.db)) as session:
        rows = sorted(session.scalars(select(ConferenceRow)), key=lambda r: r.acronym.lower())
        for row in rows:
            reviewed = REVIEWED.get(row.acronym)
            if reviewed in by_key:
                listed = by_key[reviewed]
                m = Match(reviewed, Entry(listed["title"], listed.get("description") or "", reviewed), 1.0, "")
            else:
                m = match(row.acronym, row.name, entries)
            if not m.rank:
                if m.note != "not listed":
                    review.append(f"{row.acronym:<16} {m.note}")
                continue
            key = m.rank
            how = " [reviewed]" if reviewed in by_key else ""
            matched.append(f"{row.acronym:<16} {key:<20} ({m.score:.2f}) {m.entry.name}{how}")
            accept = acceptance_for(key, acc_data)
            if accept and merge_acceptance_rates(row.acceptance_rates, accept) != row.acceptance_rates:
                records.append({"id": row.id, "acceptance_rates": accept})
            fills, notes = plan_row(row, by_key[key])
            disagreements += notes
            if fills:
                decisions.append(
                    f"{row.acronym:<16} " + ", ".join(f"{k}={v}" for k, v in fills.items())
                )
                if not args.dry_run:
                    apply_edition_fields(row, fills)
        if not args.dry_run:
            session.commit()

    print(f"Matched series ({len(matched)}):")
    print("\n".join("  " + line for line in matched) or "  none")
    print(f"\nNot matched, for review ({len(review)}):")
    print("\n".join("  " + line for line in review) or "  none")
    print(f"\nAcceptance rates to write: {len(records)} series")
    print(f"\nDecision dates to fill ({len(decisions)}):")
    print("\n".join("  " + line for line in decisions) or "  none")
    print(f"\nDeadline cross-check, not written ({len(disagreements)}):")
    print("\n".join("  " + line for line in disagreements) or "  none")
    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0
    written = merge_records(records, db_url=args.db) if records else 0
    print(f"\nWrote acceptance rates for {written} series and decision dates for {len(decisions)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
