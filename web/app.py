"""FastAPI app serving the conference table and boolean search.

Endpoints:
- ``GET /``             — the single-page table UI.
- ``GET /api/search``   — boolean search; JSON or CSV.
- ``GET /api/fields``   — queryable fields and aliases (for the UI help panel).
- ``GET /api/calendar.ics`` — subscribable iCalendar feed (no auth/credentials).

Run locally with::

    uvicorn web.app:app --reload
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.database import ConferenceRow, _row_to_model, get_engine, seed_conferences
from web.search import QueryError, build_filter, field_help

app = FastAPI(title="Conference Agent", description="Curated conference table + calendar sync.")

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Columns returned and exported, in display order.
_RESULT_COLUMNS = [
    "id",
    "acronym",
    "name",
    "category",
    "location",
    "reputation",
    "remote_option",
    "cost",
    "url",
    "upcoming_abstract_deadline",
    "upcoming_paper_deadline",
    "upcoming_start_date",
    "upcoming_end_date",
    "prior_abstract_deadline",
    "prior_paper_deadline",
    "prior_start_date",
    "prior_end_date",
    "notes",
]

# Columns that may be used for sorting.
_SORTABLE = {
    "acronym",
    "name",
    "category",
    "location",
    "reputation",
    "remote_option",
    "upcoming_start_date",
    "upcoming_abstract_deadline",
    "upcoming_paper_deadline",
}

# Date sort columns fall back to the prior edition's value, matching what the
# table actually displays (upcoming ?? prior ?? "—"). Sorting on the bare
# upcoming_* column alone bucketed every row whose upcoming date is not yet
# announced into the NULLs-last group, even though the table shows its prior
# date — so those rows appeared interleaved with the genuinely-empty ones.
_DATE_SORT_FALLBACK = {
    "upcoming_start_date": "prior_start_date",
    "upcoming_abstract_deadline": "prior_abstract_deadline",
    "upcoming_paper_deadline": "prior_paper_deadline",
}


def get_db_url() -> str:
    """Database URL from the environment, falling back to the project default."""
    return os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL)


# Auto-seed the catalog once per process. Mangum runs with lifespan="off", so a
# FastAPI startup event would not fire on Lambda; instead we seed lazily on the
# first request that touches the database (once per cold container). Seeding only
# inserts missing rows, so it never clobbers data filled in by discovery.
_seeded = False


def _ensure_seeded() -> None:
    global _seeded
    if _seeded:
        return
    seed_conferences(get_db_url())
    _seeded = True


def _row_to_dict(row: ConferenceRow) -> dict:
    """Serialize a row to a JSON-friendly dict (dates as ISO strings)."""
    out: dict = {}
    for col in _RESULT_COLUMNS:
        value = getattr(row, col)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        out[col] = value
    return out


def _run_search(query: str, sort: str, order: str) -> List[ConferenceRow]:
    _ensure_seeded()
    try:
        filt = build_filter(query)
    except QueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stmt = select(ConferenceRow)
    if filt is not None:
        stmt = stmt.where(filt)

    if sort not in _SORTABLE:
        sort = "upcoming_start_date"
    primary = getattr(ConferenceRow, sort)
    fallback = _DATE_SORT_FALLBACK.get(sort)
    # Sort on the displayed value: the upcoming date, falling back to the prior
    # one. Only rows with neither date are NULL here, so only they sort last.
    sort_col = func.coalesce(primary, getattr(ConferenceRow, fallback)) if fallback else primary
    descending = order == "desc"
    # NULLs always last, regardless of direction.
    stmt = stmt.order_by(sort_col.is_(None), sort_col.desc() if descending else sort_col.asc())

    engine = get_engine(get_db_url())
    with Session(engine) as session:
        return list(session.scalars(stmt))


@app.get("/")
def index() -> FileResponse:
    # no-cache: the browser must revalidate the single-page shell on every load,
    # so a redeploy is never masked by a stale cached copy. The (hashed-by-path)
    # /static assets and /api responses are unaffected.
    return FileResponse(
        str(_STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/fields")
def api_fields() -> dict:
    return field_help()


@app.get("/api/search")
def api_search(
    q: str = Query("", description="Boolean query. Empty matches everything."),
    sort: str = Query("upcoming_start_date"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    rows = _run_search(q, sort, order)

    if format == "csv":
        return _csv_response(rows)

    return JSONResponse([_row_to_dict(r) for r in rows])


def _csv_response(rows: List[ConferenceRow]) -> StreamingResponse:
    def generate():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=_RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for row in rows:
            writer.writerow(_row_to_dict(row))
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    headers = {"Content-Disposition": 'attachment; filename="conferences.csv"'}
    return StreamingResponse(generate(), media_type="text/csv", headers=headers)


@app.get("/api/calendar.ics")
def api_calendar_ics(
    q: str = Query("", description="Boolean query selecting the conferences to include."),
    ids: str = Query("", description="Comma-separated conference ids; overrides q when set."),
):
    """Serve the selected conferences as a subscribable iCalendar feed.

    No authentication and no Google credentials: a user adds this URL to Google
    Calendar ("Add by URL"), Apple Calendar, or Outlook, or downloads it as a
    one-off ``.ics``. The feed mirrors the active search ``q`` (or an explicit
    ``ids`` list), so a filtered view becomes a filtered calendar.
    """
    _ensure_seeded()
    engine = get_engine(get_db_url())

    if ids:
        wanted = [i.strip().upper() for i in ids.split(",") if i.strip()]
        stmt = select(ConferenceRow).where(ConferenceRow.id.in_(wanted))
        with Session(engine) as session:
            rows = list(session.scalars(stmt))
    else:
        rows = _run_search(q, "upcoming_start_date", "asc")

    conferences = [_row_to_model(r) for r in rows]

    # Pure-Python; no optional Google libraries and no credentials required.
    from conference_agent import calendar_sync

    ics = calendar_sync.conferences_to_ics(conferences)
    headers = {
        "Content-Disposition": 'inline; filename="conferences.ics"',
        # Subscribers re-fetch periodically; don't let a CDN pin a stale feed.
        "Cache-Control": "no-cache",
    }
    return Response(content=ics, media_type="text/calendar; charset=utf-8", headers=headers)
