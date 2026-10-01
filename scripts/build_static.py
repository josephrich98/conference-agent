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
      data/conferences.json   # the catalog snapshot (minus retired series) + field metadata
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

from seo_pages import slugify, write_pages
from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import ConferenceRow, former_ids, get_engine, seed_conferences
from conference_agent.refresh import is_retired
from web.app import _RESULT_COLUMNS, _row_to_dict
from web.search import field_help

# Static assets copied verbatim into the bundle (the UI is the product now).
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
_ASSETS = ("index.html", "search.js", "calendar.js", "nl_query.js")
# Vercel Functions (subscribe / confirm / unsubscribe) and their dependencies.
_VERCEL_DIR = _STATIC_DIR.parent / "vercel"


def _export_rows(
    db_url: str, include_retired: bool = False, today: date | None = None
) -> list[dict]:
    """Conference rows as JSON-friendly dicts (same shape as ``/api/search``).

    Retired series (:func:`conference_agent.refresh.is_retired`: no new edition
    within ``CHECK_WINDOW_MAX_MONTHS`` of the last one) are left out unless
    ``include_retired`` is set; their rows stay in the database. Ordered by the
    table's default sort (conference acronym, falling back to the name) so the
    first paint is sensible before the user re-sorts in the browser.
    """
    seed_conferences(db_url)
    engine = get_engine(db_url)
    today = today or date.today()
    with Session(engine) as session:
        rows = [
            r
            for r in session.scalars(select(ConferenceRow))
            if include_retired or not is_retired(r, today)
        ]
    dicts = [_row_to_dict(r) for r in rows]
    dicts.sort(key=lambda d: d.get("acronym") or d.get("name") or "")
    return dicts


def _write_redirects(db_url: str, rows: list[dict], out_dir: Path) -> int:
    """Write ``vercel.json`` redirecting former ``/c/<id>/`` pages to current ones.

    Ids are slugs of names, so a page moves when its series is renamed (and every
    page moved once, off the acronym ids). Each exported series' former ids get a
    permanent redirect, so links and search-engine entries keep working. A former
    id that is now another series' id is left alone. Returns the redirect count.
    """
    current = {r["id"] for r in rows}
    redirects = []
    for old_id, new_id in sorted(former_ids(db_url).items()):
        old_path = slugify(old_id)
        if new_id not in current or old_path == new_id or old_path in current:
            continue
        for source in (f"/c/{old_path}", f"/c/{old_path}/"):
            redirects.append({"source": source, "destination": f"/c/{new_id}/", "permanent": True})
    (out_dir / "vercel.json").write_text(
        json.dumps({"redirects": redirects}, indent=1) + "\n", encoding="utf-8"
    )
    return len(redirects) // 2


def build(db_url: str, out_dir: Path, include_retired: bool = False) -> int:
    """Write the static bundle to ``out_dir``; return the row count exported."""
    rows = _export_rows(db_url, include_retired)

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
    _write_redirects(db_url, rows, out_dir)
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
    parser.add_argument(
        "--include-retired",
        action="store_true",
        help="Also export series with no new edition within the check window.",
    )
    args = parser.parse_args()

    count = build(args.db, args.out, args.include_retired)
    print(f"Wrote {count} conference(s) to {args.out}/ (data/conferences.json + UI assets).")


if __name__ == "__main__":
    main()
