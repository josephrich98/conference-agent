"""SQLAlchemy persistence for conference records.

Provides the ORM model, engine wiring, and idempotent upsert/query helpers. The
same ORM runs against SQLite (local) or any SQLAlchemy backend with only a
connection-string change. This is a stub: the table mapping and helpers are
declared but not implemented.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from conference_agent.config import DEFAULT_DATABASE_URL
from conference_agent.models import Conference


def get_engine(db_url: str = DEFAULT_DATABASE_URL):
    """Create (and lazily initialize) a SQLAlchemy engine for `db_url`.

    Not yet implemented.
    """
    raise NotImplementedError


def upsert_conferences(
    conferences: Iterable[Conference], db_url: str = DEFAULT_DATABASE_URL
) -> int:
    """Insert or update conference rows, keyed on `Conference.id`.

    Idempotent: re-running discovery updates existing rows rather than
    duplicating them. Returns the number of rows written.

    Not yet implemented.
    """
    raise NotImplementedError


def query_conferences(
    tier: Optional[str] = None,
    year: Optional[int] = None,
    db_url: str = DEFAULT_DATABASE_URL,
) -> List[Conference]:
    """Return stored conferences, optionally filtered by tier/year.

    Not yet implemented.
    """
    raise NotImplementedError
