"""Build the static site bundle for credential-free, compute-free hosting.

The deployed Conference Agent is read-only: discovery/ingestion runs offline on a
maintainer's machine, and the live site only renders a curated table, runs the
boolean search, and serves per-conference calendar files. None of that needs
per-request compute, so this script snapshots the database to a static JSON file
and copies the single-page UI (which does search / sort / CSV / .ics entirely in
the browser) into ``dist/``. The result can be served by any static host
(e.g. Cloudflare Pages), where traffic is free and unmetered -- there is no
Lambda to invoke and no database to keep running, so heavy querying cannot incur
cost.

Usage::

    python scripts/build_static.py [--db sqlite:///data/conferences.db] [--out dist]

The output is a self-contained directory::

    dist/
      index.html          # the single-page table UI (relative asset paths)
      search.js           # boolean query language, ported to run in the browser
      calendar.js         # per-row iCalendar (.ics) generation in the browser
      nl_query.js         # natural-language ("AI") search via in-browser WebLLM
      c/<id>/, field/<tag>/, sitemap.xml, robots.txt  # prerendered SEO pages
      data/conferences.json   # the catalog snapshot + queryable-field metadata
      api/ + package.json     # Vercel Functions for per-conference update emails

The ``api/`` functions (from ``web/vercel/``) are the one exception to "no
compute": they run only when a visitor subscribes, confirms, or unsubscribes,
never for browsing or search. Other static hosts serve the table unchanged; only
the ✉️ subscribe button needs them.

``dist/`` is gitignored (data-derived); regenerate it at deploy time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import date
from pathlib import Path

from seo_pages import write_pages
from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import ConferenceRow, get_engine, seed_conferences
from web.app import _RESULT_COLUMNS, _row_to_dict
from web.search import field_help

# Static assets copied verbatim into the bundle (the UI is the product now).
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
_ASSETS = ("index.html", "search.js", "calendar.js", "nl_query.js")
# Vercel Functions (subscribe / confirm / unsubscribe) and their dependencies.
_VERCEL_DIR = _STATIC_DIR.parent / "vercel"


def _export_rows(db_url: str) -> list[dict]:
    """All conference rows as JSON-friendly dicts (same shape as ``/api/search``).

    Ordered by the table's default sort (conference acronym, falling back to the
    name) so the first paint is sensible before the user re-sorts in the browser.
    """
    seed_conferences(db_url)
    engine = get_engine(db_url)
    with Session(engine) as session:
        rows = list(session.scalars(select(ConferenceRow)))
    dicts = [_row_to_dict(r) for r in rows]
    dicts.sort(key=lambda d: d.get("acronym") or d.get("name") or "")
    return dicts


def build(db_url: str, out_dir: Path) -> int:
    """Write the static bundle to ``out_dir``; return the row count exported."""
    rows = _export_rows(db_url)

    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    generated = date.today().isoformat()
    payload = {
        "generated": generated,
        "columns": _RESULT_COLUMNS,
        "fields": field_help()["fields"],
        "conferences": rows,
    }
    (data_dir / "conferences.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    for name in _ASSETS:
        shutil.copyfile(_STATIC_DIR / name, out_dir / name)

    # Crawlable pages + sitemap; inject the browse links into the home page.
    seo = write_pages(rows, out_dir, generated)
    index = out_dir / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace("<!--SEO_LINKS-->", seo["browse"]),
        encoding="utf-8",
    )
    shutil.copyfile(_VERCEL_DIR / "package.json", out_dir / "package.json")
    shutil.copytree(_VERCEL_DIR / "api", out_dir / "api", dirs_exist_ok=True)

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static site bundle.")
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL of the source database (default: project DB).",
    )
    parser.add_argument(
        "--out",
        default="dist",
        type=Path,
        help="Output directory for the static bundle (default: dist).",
    )
    args = parser.parse_args()

    count = build(args.db, args.out)
    print(f"Wrote {count} conference(s) to {args.out}/ (data/conferences.json + UI assets).")


if __name__ == "__main__":
    main()
