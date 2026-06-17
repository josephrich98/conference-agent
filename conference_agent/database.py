"""SQLAlchemy persistence for conference records.

Provides the ORM model, engine wiring, and idempotent upsert/query helpers. The
same ORM runs against SQLite (local) or any SQLAlchemy backend with only a
connection-string change.

Idempotency: rows are keyed on ``Conference.id`` (the upper-cased acronym), so
re-running discovery updates the existing series row in place rather than
inserting a duplicate. This is what lets a daily refresh roll a newly announced
edition's dates into the "upcoming" columns without creating a second RSNA row.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from sqlalchemy import (
    Date,
    Integer,
    String,
    Text,
    cast,
    create_engine,
    extract,
    func,
    select,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, column_property, mapped_column

from conference_agent.config import (
    DEFAULT_DATABASE_URL,
    SEED_CONFERENCES,
    best_seed_url,
    curated_seed_url,
    normalize_reputation,
    seed_categories_for,
)
from conference_agent.models import (
    Conference,
    ConferenceTier,
    RemoteOption,
    normalize_categories,
)


class Base(DeclarativeBase):
    pass


class ConferenceRow(Base):
    """ORM mapping of one conference series (see :class:`Conference`)."""

    __tablename__ = "conferences"

    # Natural primary key: the upper-cased acronym (Conference.id).
    id: Mapped[str] = mapped_column(String, primary_key=True)
    acronym: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # One or more tags as a comma-joined string (e.g. "radiology, machine
    # learning"). Stored as a single column so substring (``ilike``) matching in
    # the boolean search and the per-field refresh treats any tag uniformly; the
    # ``Conference`` model splits it back into a list. See ``normalize_categories``.
    category: Mapped[str] = mapped_column(String, index=True, nullable=False)

    prior_abstract_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    prior_paper_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    prior_start_date: Mapped[Optional[Date]] = mapped_column(Date)
    prior_end_date: Mapped[Optional[Date]] = mapped_column(Date)

    upcoming_abstract_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    upcoming_paper_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    upcoming_start_date: Mapped[Optional[Date]] = mapped_column(Date, index=True)
    upcoming_end_date: Mapped[Optional[Date]] = mapped_column(Date)

    location: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    remote_option: Mapped[Optional[str]] = mapped_column(String, index=True)
    cost: Mapped[Optional[str]] = mapped_column(Text)
    reputation: Mapped[Optional[str]] = mapped_column(String, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Bookkeeping for the per-conference auto-check policy (``refresh`` module):
    # the date discovery last covered this row. ``None`` means never checked,
    # which makes a freshly seeded row eligible for an initial pass. Not part of
    # the ``Conference`` model -- it is row-level scheduling state, not conference
    # data -- so the conversion helpers below deliberately leave it untouched.
    last_checked: Mapped[Optional[Date]] = mapped_column(Date)


# Derived, SQL-computed month fields (1-12). Defined as ``column_property`` rather
# than stored columns so they are always recomputed from the dates -- there is
# nothing to migrate or backfill, and they can never drift out of sync. Each
# mirrors what the table shows: the upcoming edition's date, falling back to the
# prior edition's. ``extract`` compiles to ``STRFTIME`` on SQLite and ``EXTRACT``
# on PostgreSQL, so both backends return the month as an integer.

# The dates the derived month columns are extracted from, each preferring the
# upcoming edition over the prior. Exposed at module scope so sort code can reuse
# them as tie-breakers without duplicating the logic.
conference_date_expr = func.coalesce(
    ConferenceRow.upcoming_start_date, ConferenceRow.prior_start_date
)
abstract_date_expr = func.coalesce(
    ConferenceRow.upcoming_abstract_deadline, ConferenceRow.prior_abstract_deadline
)
paper_date_expr = func.coalesce(
    ConferenceRow.upcoming_paper_deadline, ConferenceRow.prior_paper_deadline
)

ConferenceRow.conference_month = column_property(
    cast(extract("month", conference_date_expr), Integer)
)
ConferenceRow.abstract_month = column_property(
    cast(extract("month", abstract_date_expr), Integer)
)
ConferenceRow.paper_month = column_property(
    cast(extract("month", paper_date_expr), Integer)
)


# --- Conversion helpers ----------------------------------------------------

_DATE_FIELDS = (
    "prior_abstract_deadline",
    "prior_paper_deadline",
    "prior_start_date",
    "prior_end_date",
    "upcoming_abstract_deadline",
    "upcoming_paper_deadline",
    "upcoming_start_date",
    "upcoming_end_date",
)
_TEXT_FIELDS = ("name", "category", "location", "url", "cost", "notes")


def _row_to_model(row: ConferenceRow) -> Conference:
    """Build a :class:`Conference` from an ORM row."""
    data = {
        "acronym": row.acronym,
        "name": row.name,
        "category": row.category,
        "location": row.location,
        "url": row.url,
        "cost": row.cost,
        "notes": row.notes,
        "remote_option": RemoteOption(row.remote_option) if row.remote_option else None,
        "reputation": ConferenceTier(row.reputation) if row.reputation else None,
    }
    for field in _DATE_FIELDS:
        data[field] = getattr(row, field)
    return Conference(**data)


def _normalize_url(url: "str | None") -> "str | None":
    """Ensure a stored URL carries a scheme.

    Discovery sometimes returns bare domains (e.g. ``rsna.org/annual-meeting``).
    Without ``http(s)://`` the web table renders ``<a href>`` as a relative path,
    so the link 404s. Default a schemeless value to ``https://``.
    """
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def _apply_model_to_row(row: ConferenceRow, conf: Conference) -> None:
    """Copy all fields from a :class:`Conference` onto an ORM row."""
    row.acronym = conf.acronym
    for field in _TEXT_FIELDS:
        setattr(row, field, getattr(conf, field))
    for field in _DATE_FIELDS:
        setattr(row, field, getattr(conf, field))
    row.url = _normalize_url(row.url)
    row.remote_option = conf.remote_option.value if conf.remote_option else None
    row.reputation = conf.reputation.value if conf.reputation else None


# --- Engine / helpers ------------------------------------------------------


# Engines are cached per URL so warm AWS Lambda invocations reuse the connection
# pool instead of rebuilding it (and re-running create_all) on every request.
_ENGINES: dict = {}


def _ensure_columns(engine: Engine) -> None:
    """Additively reconcile existing tables with the current ORM schema.

    ``create_all`` creates missing tables but never alters existing ones, so a
    database created before a new column was added (e.g. ``last_checked``) is
    left without it and every query referencing the column fails. For each mapped
    table that already exists, ``ALTER TABLE ADD COLUMN`` any column the live
    schema is missing.

    Scoped deliberately to additive, nullable columns: that is all the project's
    schema changes have needed so far, and it lets a long-lived SQLite file (or
    managed PostgreSQL instance) roll forward in place without a migration
    framework. A non-nullable, default-less column is skipped rather than added,
    since most backends reject adding one to a populated table.
    """
    inspector = sa_inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # create_all just made it; it already matches the model
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    continue  # unsafe to add to existing rows; leave to a real migration
                ddl_type = column.type.compile(dialect=engine.dialect)
                conn.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}')
                )


def get_engine(db_url: str = DEFAULT_DATABASE_URL) -> Engine:
    """Return a cached SQLAlchemy engine for ``db_url``, ensuring tables exist.

    ``pool_pre_ping`` recycles stale connections, which matters when a managed
    PostgreSQL instance sits behind Lambda and may drop idle connections.
    """
    engine = _ENGINES.get(db_url)
    if engine is None:
        kwargs = {"future": True, "pool_pre_ping": True}
        if db_url.startswith("postgresql"):
            # Lambda handles one request at a time per container, so keep the
            # pool tiny; overflow covers brief concurrency.
            kwargs.update(pool_size=1, max_overflow=2)
        engine = create_engine(db_url, **kwargs)
        Base.metadata.create_all(engine)
        _ensure_columns(engine)
        _ENGINES[db_url] = engine
    return engine


def upsert_conferences(
    conferences: Iterable[Conference], db_url: str = DEFAULT_DATABASE_URL
) -> int:
    """Insert or update conference rows, keyed on :attr:`Conference.id`.

    Idempotent: re-running discovery updates existing rows rather than
    duplicating them. Returns the number of rows written.

    Flagship link floor: when a series has a hand-verified link in
    ``config.SEED_CONFERENCE_LINKS`` (:func:`config.curated_seed_url`), that link
    is kept regardless of what discovery found, so a refresh cannot regress a
    curated deep link to a weaker model-found URL. Series without a curated entry
    keep the discovered URL.
    """
    engine = get_engine(db_url)
    written = 0
    with Session(engine) as session:
        for conf in conferences:
            row = session.get(ConferenceRow, conf.id)
            if row is None:
                row = ConferenceRow(id=conf.id)
                session.add(row)
            _apply_model_to_row(row, conf)
            floor = curated_seed_url(conf.acronym)
            if floor:
                row.url = floor
            # Category floor: a seeded series' tags are curated and authoritative,
            # so a discovery run cannot overwrite them with model free-text.
            seed_cats = seed_categories_for(conf.acronym)
            if seed_cats:
                row.category = ", ".join(seed_cats)
            written += 1
        session.commit()
    return written


def _coerce_date(value):
    """Parse an ISO date string (or pass through a date/None) for merge ingest."""
    from datetime import date as _date

    if value is None or isinstance(value, _date):
        return value
    text = str(value).strip()
    if not text:
        return None
    return _date.fromisoformat(text)


# Fields a researched record may carry. Date fields are parsed from ISO strings;
# the enum-backed fields are validated against their controlled vocabularies.
# ``category`` is handled separately (it may arrive as a list or a delimited
# string and is normalized to the comma-joined form), so it is not in this tuple.
_MERGEABLE_TEXT_FIELDS = ("name", "location", "url", "cost", "notes")


def merge_records(
    records: Iterable[dict],
    db_url: str = DEFAULT_DATABASE_URL,
    enforce_reputation_floor: bool = True,
) -> int:
    """Merge partial researched records into existing rows without clobbering.

    Each record is a plain dict keyed by ``id`` (the upper-cased acronym). Only
    keys that are present *and* non-null/non-empty overwrite the stored value, so
    a record that carries just newly found dates leaves the row's name, url,
    category, and reputation untouched. This is the offline counterpart to
    discovery: research gathered by any means (e.g. an interactive agent's web
    search) can be folded into the table without calling the Anthropic API.

    Date fields accept ISO ``YYYY-MM-DD`` strings; ``remote_option`` and
    ``reputation`` are validated against their enums and silently ignored if
    invalid. Records whose id matches no existing row are inserted only when they
    also supply ``name`` and ``category`` (otherwise skipped). Returns the number
    of rows written.

    ``enforce_reputation_floor`` applies the house policy
    (:func:`config.normalize_reputation`: flagships are always ``big``, other
    series capped at ``medium``) to merged reputations. Discovery keeps it on so
    the model can't over-rate a conference; the manual ``add`` CLI turns it off so
    a human curator's explicit tier is stored verbatim.
    """
    engine = get_engine(db_url)
    written = 0
    with Session(engine) as session:
        for record in records:
            acronym = (record.get("id") or record.get("acronym") or "").strip()
            if not acronym:
                continue
            row_id = acronym.upper()
            row = session.get(ConferenceRow, row_id)
            if row is None:
                if not (record.get("name") and record.get("category")):
                    continue
                row = ConferenceRow(id=row_id, acronym=acronym)
                session.add(row)

            changed = False
            for field in _DATE_FIELDS:
                if field in record and record[field] not in (None, ""):
                    setattr(row, field, _coerce_date(record[field]))
                    changed = True
            for field in _MERGEABLE_TEXT_FIELDS:
                value = record.get(field)
                if value not in (None, ""):
                    setattr(row, field, str(value).strip())
                    changed = True
            # Category may be a list or a delimited string; store the normalized,
            # comma-joined form so multi-tag records merge cleanly.
            category = record.get("category")
            if category not in (None, "", []):
                cats = normalize_categories(category)
                if cats:
                    row.category = ", ".join(cats)
                    changed = True
            remote = record.get("remote_option")
            if remote not in (None, ""):
                try:
                    row.remote_option = RemoteOption(str(remote).strip().lower()).value
                    changed = True
                except ValueError:
                    pass
            reputation = record.get("reputation")
            if reputation not in (None, ""):
                try:
                    tier = ConferenceTier(str(reputation).strip().lower())
                except ValueError:
                    tier = None
                if tier is not None:
                    # Flagship reputation floor: enforce the house policy on this
                    # write path too (mirrors upsert_conferences / seed_conferences
                    # / discovery), so a researched "medium" can't demote a flagship
                    # and a non-flagship "big" is capped. Without this, merge_records
                    # was the only path that bypassed the floor. The manual `add`
                    # CLI disables the floor so a curator's explicit tier stands.
                    capped = normalize_reputation(acronym, tier) if enforce_reputation_floor else tier
                    row.reputation = capped.value if capped else None
                    changed = True
            # Flagship floor applies even when the record carried no reputation:
            # a flagship row that arrived (or was seeded) without a tier is healed
            # to "big" here, so a dates-only merge can't leave it unlabeled.
            if enforce_reputation_floor:
                current = ConferenceTier(row.reputation) if row.reputation else None
                floored = normalize_reputation(acronym, current)
                floored_value = floored.value if floored else None
                if floored_value != row.reputation:
                    row.reputation = floored_value
                    changed = True
            # Flagship link floor: a curated deep link wins over any URL a refresh
            # merged in, mirroring upsert_conferences so neither write path can
            # regress a verified link to a weaker homepage.
            floor = curated_seed_url(acronym)
            if floor and row.url != floor:
                row.url = floor
                changed = True
            # Category floor: a seeded series' tags are curated and authoritative,
            # so they win over any category the record carried (mirrors the url
            # floor and upsert_conferences). Non-seed rows keep their own category.
            seed_cats = seed_categories_for(acronym)
            if seed_cats:
                joined = ", ".join(seed_cats)
                if row.category != joined:
                    row.category = joined
                    changed = True
            if changed:
                written += 1
        session.commit()
    return written


def seed_conferences(db_url: str = DEFAULT_DATABASE_URL, overwrite: bool = False) -> int:
    """Populate the table from the static seed catalog (``config.SEED_CONFERENCES``).

    Builds a minimal :class:`Conference` for every seed -- acronym, name, category,
    the policy-normalized reputation tier, and the official URL -- leaving the
    deadline/date fields empty. This makes the table usable without the discovery
    API; a later discovery run fills the dates into the same rows in place.

    Only *missing* rows are inserted by default, so seeding never clobbers data
    already discovered; pass ``overwrite=True`` to also refresh existing rows'
    seed-derived fields. Idempotent. Returns the number of rows written.
    """
    engine = get_engine(db_url)
    written = 0
    with Session(engine) as session:
        for acronym, name, category, tier in SEED_CONFERENCES:
            conf = Conference(
                acronym=acronym,
                name=name,
                category=category,
                reputation=normalize_reputation(acronym, tier),
                url=best_seed_url(acronym),
            )
            row = session.get(ConferenceRow, conf.id)
            if row is None:
                row = ConferenceRow(id=conf.id)
                session.add(row)
            elif not overwrite:
                continue
            _apply_model_to_row(row, conf)
            written += 1
        session.commit()
    return written


def distinct_categories(db_url: str = DEFAULT_DATABASE_URL) -> set[str]:
    """Return the set of category tags currently present in the table.

    Tags are normalized (lowercased, split on ``,``/``;``) the same way the model
    stores them, so callers can compare a candidate tag against the table's
    existing vocabulary -- e.g. to warn when a manual ``add`` introduces a tag no
    other row uses.
    """
    engine = get_engine(db_url)
    cats: set[str] = set()
    with Session(engine) as session:
        for joined in session.scalars(select(ConferenceRow.category)):
            cats.update(normalize_categories(joined))
    return cats


def query_conferences(
    category: Optional[str] = None,
    reputation: Optional[str] = None,
    db_url: str = DEFAULT_DATABASE_URL,
) -> List[Conference]:
    """Return stored conferences, optionally filtered by category/reputation."""
    engine = get_engine(db_url)
    stmt = select(ConferenceRow)
    if category is not None:
        # Substring match: a row's category column may list several tags
        # (e.g. "radiology, pediatrics"), so an exact match would miss it.
        stmt = stmt.where(ConferenceRow.category.ilike(f"%{category}%"))
    if reputation is not None:
        stmt = stmt.where(ConferenceRow.reputation == reputation)
    stmt = stmt.order_by(ConferenceRow.upcoming_start_date.is_(None), ConferenceRow.upcoming_start_date)
    with Session(engine) as session:
        return [_row_to_model(row) for row in session.scalars(stmt)]
