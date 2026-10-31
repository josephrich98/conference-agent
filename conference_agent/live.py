"""Read-only access to the live site's data for `lookup` and `info`.

The deployed site publishes its full catalog as one static file,
``<SITE_URL>/data/conferences.json`` (written by ``scripts/build_static.py``).
:func:`live_database_url` downloads that snapshot and loads it into a cached
SQLite database, so the CLI's read commands run the same SQLAlchemy search over
the live data as they do over a local database. Nothing here writes to the site.

The cache lives in ``$XDG_CACHE_HOME/conference-agent`` (default
``~/.cache/conference-agent``). The first run of a day revalidates the snapshot
with its ETag, so an unchanged catalog is not downloaded again; later runs that
day use the cached copy without a request (``refresh=True``, the CLI's
``--refresh``, forces a check). A failed request falls back to the last cached
copy with a warning and is retried on the next run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Date, DateTime
from sqlalchemy.orm import Session

from conference_agent.config import SITE_URL

LIVE_DATA_URL = os.environ.get(
    "CONFERENCE_LIVE_DATA_URL", f"{SITE_URL.rstrip('/')}/data/conferences.json"
)
_TIMEOUT = 30


class LiveDataError(RuntimeError):
    """Raised when the live snapshot cannot be fetched and no cached copy exists."""


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "conference-agent"


def _fetch(cache: Path, refresh: bool = False) -> bytes:
    """The snapshot's bytes: downloaded when changed, else the cached copy.

    The site is checked at most once a day: when the cached copy was last
    confirmed current today, it is returned without a request unless ``refresh``.
    """
    body_path, etag_path = cache / "conferences.json", cache / "conferences.etag"
    checked_path = cache / "conferences.checked"
    today = date.today().isoformat()
    if (
        not refresh
        and body_path.exists()
        and checked_path.exists()
        and checked_path.read_text().strip() == today
    ):
        return body_path.read_bytes()
    request = urllib.request.Request(LIVE_DATA_URL, headers={"User-Agent": "conference-agent"})
    if body_path.exists() and etag_path.exists():
        request.add_header("If-None-Match", etag_path.read_text().strip())
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # nosec B310
            body = response.read()
            etag = response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            checked_path.write_text(today)
            return body_path.read_bytes()
        reason = f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = str(getattr(exc, "reason", exc))
    else:
        json.loads(body)  # refuse to cache a truncated or non-JSON response
        cache.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(body)
        if etag:
            etag_path.write_text(etag)
        else:
            etag_path.unlink(missing_ok=True)
        checked_path.write_text(today)
        return body
    if body_path.exists():
        print(
            f"Warning: could not reach {LIVE_DATA_URL} ({reason}); using the cached copy.",
            file=sys.stderr,
        )
        return body_path.read_bytes()
    raise LiveDataError(
        f"could not download the live data from {LIVE_DATA_URL} ({reason}). "
        "Use --local to search the local database instead."
    )


def _coerce(column, value):
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    return value


def _build(body: bytes, path: Path) -> None:
    """Load the snapshot's rows into a new SQLite database at ``path``."""
    from conference_agent.database import ConferenceRow, get_engine

    columns = {c.key: c for c in ConferenceRow.__table__.columns}
    rows = json.loads(body)["conferences"]
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    engine = get_engine(f"sqlite:///{tmp}")
    with Session(engine) as session:
        session.add_all(
            ConferenceRow(**{k: _coerce(columns[k], v) for k, v in row.items() if k in columns})
            for row in rows
        )
        session.commit()
    engine.dispose()
    tmp.replace(path)


def live_database_url(refresh: bool = False) -> str:
    """A SQLite URL holding the live site's current catalog (built on first use).

    The site is checked at most once a day; ``refresh`` checks it regardless.
    """
    cache = cache_dir()
    body = _fetch(cache, refresh)
    # Name the database by content, so a changed snapshot gets a fresh file (and
    # a fresh cached engine) while an unchanged one is reused as is.
    path = cache / f"live-{hashlib.sha256(body).hexdigest()[:16]}.db"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        _build(body, path)
        for old in cache.glob("live-*.db"):
            if old != path:
                old.unlink(missing_ok=True)
    return f"sqlite:///{path}"
