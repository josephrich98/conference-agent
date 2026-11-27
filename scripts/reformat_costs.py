"""Rewrite stored free-text costs into the tier format (``models.COST_TIERS``).

Costs researched before the tier format are free-text summaries. This pass
sends them, in batches, to an extraction-only model call (no web search;
``discover.reformat_costs``) and writes each result through
``database.merge_records``, which replaces free text with tiers. A result with
no priced tier (the text gave no fees) leaves the stored text, as does a pinned
cost. Discovery writes tiers from now on, so this is needed once.

Usage::

    python scripts/reformat_costs.py [--dry-run] [--limit N] [--batch-size N]
        [--backend claude-code|api] [--model MODEL] [--db URL]
"""

from __future__ import annotations

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.database import (
    COST_COLUMNS,
    DEFAULT_DATABASE_URL,
    ConferenceRow,
    get_engine,
    merge_records,
    pinned_fields,
)
from conference_agent.discover import DEFAULT_BACKEND, DISCOVERY_BACKENDS, reformat_costs
from conference_agent.models import is_structured_cost


def free_text_costs(db_url: str) -> dict:
    """``{"<id>|<column>": text}`` for each stored, unpinned free-text cost."""
    out = {}
    with Session(get_engine(db_url)) as session:
        for row in session.scalars(select(ConferenceRow).order_by(ConferenceRow.id)):
            pinned = set(pinned_fields(row))
            for column in COST_COLUMNS:
                text = getattr(row, column)
                if text and column not in pinned and not is_structured_cost(text):
                    out[f"{row.id}|{column}"] = text
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--dry-run", action="store_true", help="print the rewrites, write nothing")
    parser.add_argument("--limit", type=int, help="at most this many costs")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--backend", choices=DISCOVERY_BACKENDS, default=DEFAULT_BACKEND)
    parser.add_argument("--model", help="model (default: the backend's)")
    args = parser.parse_args(argv)

    pending = free_text_costs(args.db)
    keys = list(pending)[: args.limit] if args.limit else list(pending)
    print(f"{len(keys)} free-text costs to reformat")
    written = kept = 0
    for start in range(0, len(keys), args.batch_size):
        batch = {k: pending[k] for k in keys[start : start + args.batch_size]}
        try:
            results = reformat_costs(batch, backend=args.backend, model=args.model)
        except RuntimeError as exc:
            print(f"batch {start // args.batch_size + 1} failed: {exc}")
            continue
        records = []
        for key, old in batch.items():
            new = results.get(key)
            row_id, column = key.split("|")
            if not new:
                kept += 1
                print(f"= {row_id} {column}: no tiers; kept\n    {old}")
                continue
            print(f"~ {row_id} {column}\n    {old}\n  → " + new.replace("\n", "\n    "))
            records.append({"id": row_id, column: new})
        if records and not args.dry_run:
            merge_records(records, db_url=args.db)
        written += len(records)
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written}; kept {kept} as free text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
