"""Offline ingest of researched conference records into the table.

This is the API-free counterpart to ``build_table.py`` / ``daily_update.py``:
instead of calling the Anthropic discovery agent (which consumes API tokens),
it merges conference records that were gathered by some other means -- for
example, an interactive agent's own web search -- into the database.

Records are merged, not replaced: only the fields a record actually supplies
overwrite the stored row, so a record carrying just newly found dates leaves the
row's name, url, category, and reputation intact (see
``database.merge_records``).

Input is one or more JSON files, each a list of record objects (or a single
object). Each record is keyed by ``id`` (the acronym) and may carry any of:
``prior_abstract_deadline``, ``prior_paper_deadline``, ``prior_start_date``,
``prior_end_date``, ``upcoming_abstract_deadline``, ``upcoming_paper_deadline``,
``upcoming_start_date``, ``upcoming_end_date`` (ISO ``YYYY-MM-DD``), plus
``location``, ``url``, ``cost``, ``notes``, ``remote_option``, ``reputation``.
Unknown keys (e.g. ``source_url``) are ignored.

Usage:
    python scripts/ingest_records.py data/research/*.json
    python scripts/ingest_records.py --db sqlite:///data/conferences.db records.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from typing import List

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import merge_records


def _load_records(paths: List[str]) -> List[dict]:
    records: List[dict] = []
    for pattern in paths:
        matches = sorted(glob.glob(pattern)) or [pattern]
        for path in matches:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                payload = [payload]
            records.extend(payload)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge researched records into the table.")
    parser.add_argument("paths", nargs="+", help="JSON file(s) or globs of records.")
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    args = parser.parse_args()

    records = _load_records(args.paths)
    if not records:
        print("No records found in the given paths.")
        return
    written = merge_records(records, db_url=args.db)
    print(f"Merged {written} record(s) into {args.db}")


if __name__ == "__main__":
    main()
