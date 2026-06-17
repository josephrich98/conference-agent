"""Push every conference row from one database into another (idempotent upsert).

Use this to reconcile the deployment with a known-good local table: it reads all
rows from ``--source`` and upserts them into ``--target`` keyed on the conference
acronym, so the target ends up matching the source. Because it goes through
:func:`database.upsert_conferences`, the house reputation floor and the curated
url/category floors are reapplied on write -- so a flagship like ECCV lands as
``big`` regardless of what the target held before.

Idempotent and safe to re-run. Nothing is deleted: rows present only in the
target are left untouched (the source is treated as authoritative for the rows it
contains, not as a full mirror).

Usage (push the local SQLite table into RDS):
    export CONFERENCE_DATABASE_URL="postgresql+psycopg://conf_admin:<pw>@<endpoint>:5432/conferences"
    python scripts/push_db.py \
        --source sqlite:///data/conferences.db \
        --target "$CONFERENCE_DATABASE_URL"

Dry-run (report the row count without writing):
    python scripts/push_db.py --source sqlite:///data/conferences.db --target "$URL" --dry-run
"""

from __future__ import annotations

import argparse
import os

from sqlalchemy import make_url, select
from sqlalchemy.orm import Session

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import (
    ConferenceRow,
    _row_to_model,
    get_engine,
    upsert_conferences,
)


def _redact(db_url: str) -> str:
    """Return a display form of a DB URL with any password masked."""
    try:
        url = make_url(db_url)
    except Exception:
        return db_url
    if url.password:
        url = url.set(password="***")
    return url.render_as_string(hide_password=False)


def _read_all(source_url: str):
    engine = get_engine(source_url)
    with Session(engine) as session:
        return [_row_to_model(row) for row in session.scalars(select(ConferenceRow))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default="sqlite:///data/conferences.db",
        help="Source DB URL to copy rows from (default: local SQLite).",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Target DB URL to upsert rows into (default: $CONFERENCE_DATABASE_URL).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the row count without writing to the target.",
    )
    args = parser.parse_args()

    if args.source == args.target:
        parser.error("--source and --target are the same database; nothing to push.")

    conferences = _read_all(args.source)
    print(f"Read {len(conferences)} rows from {_redact(args.source)}")
    if args.dry_run:
        print("Dry run: no rows written.")
        return

    written = upsert_conferences(conferences, db_url=args.target)
    print(f"Upserted {written} rows into {_redact(args.target)}")


if __name__ == "__main__":
    main()
