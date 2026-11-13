"""Fill the CCF and CORE ranking columns from the official published lists.

The two rankings are official lists, so they are copied, never researched or
guessed:

- **CORE**: the ICORE conference ranking (current edition ``ICORE2026``),
  exported as CSV from the ICORE portal (portal.core.edu.au/conf-ranks). Only
  the A* / A / B / C ranks are kept; National, Regional, Australasian,
  Unranked, and Journal Published entries count as unranked.
- **CCF**: the China Computer Federation's recommended list of international
  conferences (7th edition, 2022, with the 2024 name revisions), read from the
  ten area pages on www.ccf.org.cn/Academic_Evaluation (the conference half of
  each page; the journal half is ignored).

``--download`` fetches both into ``data/rankings/`` (gitignored):
``core_icore2026.csv`` and ``ccf_2022.csv`` (acronym, name, rank, area). Without
it the saved files are used, so a run is reproducible offline.

Matching is deliberately strict. A stored series gets a rank only when a list
entry has the same acronym (case and punctuation ignored) **and** a similar
name (token overlap of the names with generic words such as "international",
"conference", "ACM", "IEEE" removed, at least ``NAME_THRESHOLD``). Acronym-only
and name-only matches, and acronyms with several matching entries of different
ranks, are reported and left blank, except the few reviewed equivalences in
``CONFIRMED`` (a venue listed under a former name or its SIG's acronym), which
still require the listed acronym and name to be present. Values are written with
``database.merge_records`` (the ``add --update`` path), which only fills or
changes a rank and never clears one.

Usage::

    python scripts/fill_rankings.py [--download] [--dry-run] [--db URL]
"""

from __future__ import annotations

import argparse
import csv
import difflib
import html
import re
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conference_agent.config import DEFAULT_DATABASE_URL  # noqa: E402
from conference_agent.models import CORE_EDITION, normalize_core_rank  # noqa: E402

RANKINGS_DIR = ROOT / "data" / "rankings"
CORE_FILE = RANKINGS_DIR / "core_icore2026.csv"
CCF_FILE = RANKINGS_DIR / "ccf_2022.csv"

CORE_URL = (
    "https://portal.core.edu.au/conf-ranks/?search=&by=all&source="
    f"{CORE_EDITION}&sort=atitle&page=1&do=Export"
)
CCF_BASE = "https://www.ccf.org.cn/Academic_Evaluation/"
# The ten CCF areas (page slug -> English area name).
CCF_AREAS = {
    "ARCH_DCP_SS": "Computer architecture / parallel and distributed computing / storage",
    "CN": "Computer networks",
    "NIS": "Network and information security",
    "TCSE_SS_PDL": "Software engineering / system software / programming languages",
    "DM_CS": "Databases / data mining / information retrieval",
    "TCS": "Theoretical computer science",
    "CGAndMT": "Computer graphics and multimedia",
    "AI": "Artificial intelligence",
    "HCIAndPC": "Human-computer interaction and pervasive computing",
    "Cross_Compre_Emerging": "Interdisciplinary / comprehensive / emerging",
}
_USER_AGENT = "Mozilla/5.0 (compatible; conference-agent rankings fill)"

# Minimum name similarity (0-1) for an acronym match to count.
NAME_THRESHOLD = 0.5
# A name this similar with a different acronym is reported, not applied.
NAME_ONLY_REPORT = 0.8

_GENERIC_WORDS = {
    "the", "of", "on", "in", "and", "for", "a", "an", "to", "at",
    "international", "conference", "annual", "meeting", "symposium", "proceedings",
    "acm", "ieee", "siam", "cvf", "rsj", "usenix", "aaai", "sigplan", "sigkdd", "sigact",
    "joint", "advances", "european", "asian",
}  # fmt: skip


# Reviewed equivalences the automatic rule cannot see: (column, stored acronym)
# -> (listed acronym, start of the listed name). Applied only while the list
# still carries that entry.
CONFIRMED = {
    # CCF lists KDD under its SIG's acronym.
    ("ccf_rank", "KDD"): ("SIGKDD", "ACM SIGKDD Conference on Knowledge Discovery"),
    # CCF names SIGGRAPH by the SIG rather than the conference.
    ("ccf_rank", "SIGGRAPH"): ("SIGGRAPH", "ACM Special Interest Group on Computer Graphics"),
    # NAACL was renamed "Nations of the Americas Chapter" in 2024; both lists
    # keep the former "North American" name.
    ("ccf_rank", "NAACL"): ("NAACL", "North American Chapter of the Association"),
    ("core_rank", "NAACL"): ("NAACL", "North American Association for Computational Linguistics"),
    # CORE lists AAAI under its former name.
    ("core_rank", "AAAI"): ("AAAI", "National Conference of the American Association for Artificial Intelligence"),
}


@dataclass
class Entry:
    acronym: str
    name: str
    rank: str
    area: str = ""


# --- download ---------------------------------------------------------------


def _fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:  # nosec B310 - https checked above
        return response.read()


def parse_core_csv(text: str) -> list[Entry]:
    """The ICORE export (no header: id, title, acronym, source, rank, ...)."""
    entries = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 5:
            continue
        entries.append(Entry(acronym=row[2].strip(), name=" ".join(row[1].split()), rank=row[4].strip()))
    return entries


_CCF_ITEM = re.compile(
    r'<li>\s*<div[^>]*>(?P<n>[^<]*)</div>\s*<div class="sname"[^>]*>(?P<acr>.*?)</div>\s*'
    r"<div[^>]*>(?P<name>.*?)</div>",
    re.S,
)
_CCF_RANK = re.compile(r'class="m-tit1">\s*([ABC])类')


def parse_ccf_page(page: str, area: str) -> list[Entry]:
    """The conference entries of one CCF area page."""
    start = page.find("推荐国际学术会议")
    if start < 0:
        raise ValueError(f"no conference section on the CCF page for {area}")
    section = page[start:]
    # Each rank heading starts a block; the items follow it until the next.
    marks = [(m.start(), m.group(1)) for m in _CCF_RANK.finditer(section)]
    entries = []
    for i, (pos, rank) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(section)
        for m in _CCF_ITEM.finditer(section, pos, end):
            if not m.group("n").strip().isdigit():
                continue  # the column-heading row
            strip = lambda s: " ".join(html.unescape(re.sub(r"<[^>]+>", "", s)).split())  # noqa: E731
            entries.append(Entry(acronym=strip(m.group("acr")), name=strip(m.group("name")), rank=rank, area=area))
    return entries


def download() -> None:
    RANKINGS_DIR.mkdir(parents=True, exist_ok=True)
    core = _fetch(CORE_URL).decode("utf-8", errors="replace")
    if len(parse_core_csv(core)) < 500:
        raise SystemExit("The ICORE export looks incomplete; not saved.")
    CORE_FILE.write_text(core, encoding="utf-8")
    entries: list[Entry] = []
    for slug, area in CCF_AREAS.items():
        page = _fetch(CCF_BASE + slug + "/").decode("utf-8", errors="replace")
        found = parse_ccf_page(page, area)
        if not found:
            raise SystemExit(f"No conferences parsed from the CCF {slug} page; not saved.")
        entries += found
    with CCF_FILE.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["acronym", "name", "rank", "area"])
        for e in entries:
            writer.writerow([e.acronym, e.name, e.rank, e.area])
    print(f"Saved {CORE_FILE.relative_to(ROOT)} and {CCF_FILE.relative_to(ROOT)} ({len(entries)} CCF conferences).")


def load_core() -> list[Entry]:
    entries = []
    for e in parse_core_csv(CORE_FILE.read_text(encoding="utf-8")):
        e.rank = normalize_core_rank(e.rank) or ""
        entries.append(e)
    return entries


def load_ccf() -> list[Entry]:
    with CCF_FILE.open(encoding="utf-8") as fh:
        return [Entry(r["acronym"], r["name"], r["rank"], r["area"]) for r in csv.DictReader(fh)]


# --- matching ---------------------------------------------------------------


def norm_acronym(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def name_tokens(value: str) -> set[str]:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\(.*?\)", " ", text.replace("&", " and "))
    words = re.findall(r"[a-z0-9]+", text)
    return {w for w in words if w not in _GENERIC_WORDS}


def name_similarity(a: str, b: str) -> float:
    """Token overlap of the names, or their character similarity when higher
    (the CCF pages run some words together: "ArtificialIntelligence")."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    tokens = len(ta & tb) / len(ta | tb)
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    chars = difflib.SequenceMatcher(None, squash(a), squash(b)).ratio()
    return max(tokens, chars if chars >= 0.92 else 0.0)


@dataclass
class Match:
    rank: str | None  # None: not applied
    entry: Entry | None
    score: float
    note: str  # "" when applied, else why not


def match(acronym: str, name: str, entries: list[Entry], field: str = "") -> Match:
    """Find a series' rank in one list (see the module docstring)."""
    confirmed = CONFIRMED.get((field, acronym))
    if confirmed:
        listed, prefix = confirmed
        hits = [e for e in entries if e.acronym == listed and e.name.startswith(prefix)]
        if len(hits) == 1 and hits[0].rank:
            return Match(hits[0].rank, hits[0], name_similarity(name, hits[0].name), "")
    key = norm_acronym(acronym)
    same = [e for e in entries if key and norm_acronym(e.acronym) == key]
    scored = sorted(((name_similarity(name, e.name), e) for e in same), key=lambda t: -t[0])
    good = [(s, e) for s, e in scored if s >= NAME_THRESHOLD]
    if good:
        ranks = {e.rank for _, e in good}
        score, best = good[0]
        if len(ranks) > 1:
            return Match(None, best, score, "ambiguous: " + "; ".join(f"{e.name} ({e.rank or 'unranked'})" for _, e in good))
        if not best.rank:
            return Match(None, best, score, "listed but not ranked A*-C")
        return Match(best.rank, best, score, "")
    if scored:
        score, best = scored[0]
        return Match(None, best, score, f"acronym only: listed name '{best.name}'")
    # Report a near-identical name under another acronym for review.
    near = max(((name_similarity(name, e.name), e) for e in entries), key=lambda t: t[0], default=(0, None))
    if near[1] is not None and near[0] >= NAME_ONLY_REPORT:
        return Match(None, near[1], near[0], f"name only: listed as {near[1].acronym or '(no acronym)'}")
    return Match(None, None, 0.0, "not listed")


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--download", action="store_true", help="Fetch the official lists into data/rankings/ first")
    parser.add_argument("--dry-run", action="store_true", help="Report the matches without writing")
    parser.add_argument("--db", default=DEFAULT_DATABASE_URL, help="Database URL (default %(default)s)")
    args = parser.parse_args(argv)

    if args.download:
        download()
    if not (CORE_FILE.exists() and CCF_FILE.exists()):
        print("The ranking lists are missing; run with --download.", file=sys.stderr)
        return 1
    lists = {"ccf_rank": load_ccf(), "core_rank": load_core()}

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow, get_engine, merge_records

    with Session(get_engine(args.db)) as session:
        rows = [(r.id, r.acronym, r.name, r.ccf_rank, r.core_rank) for r in session.scalars(select(ConferenceRow))]

    records, applied, review, changed = [], [], [], []
    for row_id, acronym, name, ccf, core in sorted(rows, key=lambda r: r[1].lower()):
        stored = {"ccf_rank": ccf, "core_rank": core}
        record = {"id": row_id}
        for field, entries in lists.items():
            m = match(acronym, name, entries, field)
            label = field.removesuffix("_rank").upper()
            if m.rank:
                how = " [confirmed]" if (field, acronym) in CONFIRMED else ""
                applied.append(f"{acronym:<16} {label:<4} {m.rank:<2}  ({m.score:.2f}) {m.entry.name}{how}")
                if stored[field] != m.rank:
                    record[field] = m.rank
                    if stored[field]:
                        changed.append(f"{acronym} {label}: {stored[field]} -> {m.rank}")
            elif m.note != "not listed":
                review.append(f"{acronym:<16} {label:<4} {m.note}")
        if len(record) > 1:
            records.append(record)

    print(f"Matched ranks ({len(applied)}):")
    print("\n".join("  " + line for line in applied) or "  none")
    print(f"\nLeft blank, for review ({len(review)}):")
    print("\n".join("  " + line for line in review) or "  none")
    if changed:
        print("\nStored ranks that differ from the lists (updated):")
        print("\n".join("  " + line for line in changed))
    ccf_n = sum(1 for line in applied if " CCF " in line)
    print(f"\nCCF: {ccf_n} series, CORE: {len(applied) - ccf_n} series, "
          f"{len(records)} row(s) to write.")
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    written = merge_records(records, db_url=args.db) if records else 0
    print(f"Wrote {written} row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
