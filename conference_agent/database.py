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
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from conference_agent.config import (
    DEFAULT_DATABASE_URL,
    HARDCODED_FORMATS,
    SEED_CONFERENCES,
    best_seed_url,
    curated_seed_url,
    seed_subcategories_for,
)
from conference_agent.models import (
    Conference,
    RemoteOption,
    categories_for_subcategories,
    normalize_formats,
    normalize_subcategories,
    size_for_attendance,
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
    # One or more granular subcategory tags as a comma-joined string (e.g.
    # "radiology, machine learning"). Stored as a single column so substring
    # (``ilike``) matching in the boolean search and the per-field refresh treats
    # any tag uniformly; the ``Conference`` model splits it back into a list. See
    # ``normalize_subcategories``.
    subcategory: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # Broad top-level category (one or more of ``models.CATEGORIES``) as a
    # comma-joined string, *derived* from ``subcategory`` via
    # ``models.SUBCATEGORY_TO_CATEGORY`` -- never accepted as input. Stored
    # denormalized (like ``size``) so the search/sort can query it as a column,
    # but only ever written by the derivation, so it can't drift from the
    # subcategories. NULL when no subcategory maps to a category.
    category: Mapped[Optional[str]] = mapped_column(String, index=True)

    prior_abstract_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    prior_paper_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    prior_start_date: Mapped[Optional[Date]] = mapped_column(Date)
    prior_end_date: Mapped[Optional[Date]] = mapped_column(Date)
    # Registration is free text (windows like "Early bird: ...; Regular: ..."),
    # not a date -- a meeting may publish several windows, only an opening date, or
    # nothing at all, so a single Date column cannot represent it. Stored as text;
    # there is consequently no derived registration month.
    prior_registration: Mapped[Optional[str]] = mapped_column(Text)

    upcoming_abstract_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    upcoming_paper_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    upcoming_start_date: Mapped[Optional[Date]] = mapped_column(Date, index=True)
    upcoming_end_date: Mapped[Optional[Date]] = mapped_column(Date)
    upcoming_registration: Mapped[Optional[str]] = mapped_column(Text)

    location: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    remote_option: Mapped[Optional[str]] = mapped_column(String, index=True)
    # Submission/presentation formats offered (abstract, paper, poster, oral) as a
    # comma-joined string, mirroring ``category``. Stored as one column so the
    # boolean search can substring-match any format uniformly; the ``Conference``
    # model splits it back into a list. Indexed for that filtering; NULL when unknown.
    format: Mapped[Optional[str]] = mapped_column(String, index=True)
    cost: Mapped[Optional[str]] = mapped_column(Text)
    # Attendance is the objective input; ``size`` is the bucket derived from it
    # (see ``models.size_for_attendance``). ``size`` is stored denormalized so the
    # search/filter machinery can query it as a column, but it is only ever set by
    # the bucketing function on write -- never accepted as input -- so it can never
    # drift from the attendance figure. ``attendance_source`` is provenance kept
    # internal (it is not returned by the public API/CSV).
    attendance: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    attendance_year: Mapped[Optional[int]] = mapped_column(Integer)
    attendance_source: Mapped[Optional[str]] = mapped_column(Text)
    size: Mapped[Optional[str]] = mapped_column(String, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Bookkeeping for the per-conference auto-check policy (``refresh`` module):
    # the date discovery last covered this row. ``None`` means never checked,
    # which makes a freshly seeded row eligible for an initial pass. Not part of
    # the ``Conference`` model -- it is row-level scheduling state, not conference
    # data -- so the conversion helpers below deliberately leave it untouched.
    last_checked: Mapped[Optional[Date]] = mapped_column(Date)

    # Derived month-of-year fields (1-12), stored as real columns so they exist in
    # the database file itself (browsable/queryable outside the ORM). Like ``size``
    # and ``category`` they are denormalized but never accepted as input:
    # ``_apply_model_to_row`` writes each from the ``Conference`` model's matching
    # ``*_month`` property -- the month of the upcoming date, falling back to the
    # prior one -- so they cannot drift from the dates. ``recompute_months``
    # re-derives them all in place (e.g. to backfill rows stored before the columns
    # existed). NULL when the underlying date is unset.
    conference_month: Mapped[Optional[int]] = mapped_column(Integer)
    abstract_month: Mapped[Optional[int]] = mapped_column(Integer)
    paper_month: Mapped[Optional[int]] = mapped_column(Integer)


# The dates the stored month columns are derived from, each preferring the
# upcoming edition over the prior. Exposed at module scope so the sort code can
# reuse them as month-sort tie-breakers without duplicating the coalesce logic.
conference_date_expr = func.coalesce(
    ConferenceRow.upcoming_start_date, ConferenceRow.prior_start_date
)
abstract_date_expr = func.coalesce(
    ConferenceRow.upcoming_abstract_deadline, ConferenceRow.prior_abstract_deadline
)
paper_date_expr = func.coalesce(
    ConferenceRow.upcoming_paper_deadline, ConferenceRow.prior_paper_deadline
)
# Registration is free text (see ``ConferenceRow.prior_registration``), so unlike
# the deadline/date fields it has no coalesced date expression and no derived
# month column.


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
# ``subcategory`` is the stored granular column; ``category`` is derived from it
# (written separately, like ``size``), so it is not in this round-trip tuple.
# ``prior_registration`` / ``upcoming_registration`` are free text (not dates), so
# they ride along with the other text fields here rather than in ``_DATE_FIELDS``.
_TEXT_FIELDS = (
    "name",
    "subcategory",
    "location",
    "url",
    "cost",
    "attendance_source",
    "notes",
    "prior_registration",
    "upcoming_registration",
)


def _row_to_model(row: ConferenceRow) -> Conference:
    """Build a :class:`Conference` from an ORM row."""
    data = {
        "acronym": row.acronym,
        "name": row.name,
        "subcategory": row.subcategory,
        "format": row.format,
        "location": row.location,
        "url": row.url,
        "cost": row.cost,
        "notes": row.notes,
        "prior_registration": row.prior_registration,
        "upcoming_registration": row.upcoming_registration,
        "remote_option": RemoteOption(row.remote_option) if row.remote_option else None,
        "attendance": row.attendance,
        "attendance_year": row.attendance_year,
        "attendance_source": row.attendance_source,
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
    # Category is derived from the subcategories, never taken as input -- so it
    # always matches. NULL when no subcategory maps to a category.
    row.category = conf.category or None
    row.url = _normalize_url(row.url)
    row.remote_option = conf.remote_option.value if conf.remote_option else None
    # Store the joined formats, collapsing an empty list to NULL so the presence
    # test (``format:*``) and search treat "no formats recorded" as unset.
    row.format = conf.format or None
    row.attendance = conf.attendance
    row.attendance_year = conf.attendance_year
    # Size is derived from attendance, never taken as input -- so it always matches.
    row.size = conf.size.value if conf.size else None
    # Month-of-year fields are derived from the dates, never taken as input (like
    # size/category) -- write them from the model's computed properties so the
    # stored columns always match the dates.
    row.conference_month = conf.conference_month
    row.abstract_month = conf.abstract_month
    row.paper_month = conf.paper_month


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


def _migrate_category_to_subcategory(engine: Engine) -> bool:
    """Rename a legacy ``category`` column to ``subcategory`` in place.

    The granular tag column was renamed ``category`` -> ``subcategory`` when the
    broad, derived ``category`` was introduced. ``_ensure_columns`` only *adds*
    columns, so it cannot perform this rename; without it, a database created
    before the rename would have a ``category`` column (holding the granular tags)
    and no ``subcategory`` column, and every query would fail. Both SQLite (>=3.25)
    and PostgreSQL support ``ALTER TABLE ... RENAME COLUMN``.

    Idempotent: only renames when the table has the legacy ``category`` column and
    no ``subcategory`` column yet. Returns ``True`` when a rename was performed (so
    the caller can backfill the new derived ``category`` column afterward).
    """
    inspector = sa_inspect(engine)
    if not inspector.has_table(ConferenceRow.__tablename__):
        return False
    columns = {col["name"] for col in inspector.get_columns(ConferenceRow.__tablename__)}
    if "category" in columns and "subcategory" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {ConferenceRow.__tablename__} "
                    "RENAME COLUMN category TO subcategory"
                )
            )
        return True
    return False


def _migrate_registration_date_to_text(engine: Engine) -> None:
    """Rename the legacy registration *date* columns to free-text columns in place.

    Registration was reworked from a single date per edition to a free-text field
    (windows like "Early bird: ...; Regular: ..."), so ``*_registration_date``
    (Date) became ``*_registration`` (Text). ``_ensure_columns`` only *adds*
    columns, so without this a database created before the rework would keep the
    old date columns and lack the new text ones, and every query would fail.

    Idempotent: only renames a legacy column that is still present and whose new
    name does not yet exist. The columns were unused (registration was never
    populated), so on PostgreSQL the column type is additionally widened to text;
    SQLite's dynamic typing needs no type change.
    """
    inspector = sa_inspect(engine)
    if not inspector.has_table(ConferenceRow.__tablename__):
        return
    columns = {col["name"] for col in inspector.get_columns(ConferenceRow.__tablename__)}
    renames = (
        ("prior_registration_date", "prior_registration"),
        ("upcoming_registration_date", "upcoming_registration"),
    )
    table = ConferenceRow.__tablename__
    with engine.begin() as conn:
        for old, new in renames:
            if old in columns and new not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                )
                if engine.dialect.name != "sqlite":
                    conn.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN {new} TYPE text")
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
        # Rename the legacy granular column before the additive reconcile adds the
        # new derived ``category`` column alongside it.
        migrated = _migrate_category_to_subcategory(engine)
        # Rename the legacy registration *date* columns to the new free-text
        # columns before the additive reconcile, so it sees them already present.
        _migrate_registration_date_to_text(engine)
        # Detect a pre-existing table missing the stored month columns *before* the
        # additive reconcile adds them, so we know to backfill them afterward (a
        # fresh table is created by ``create_all`` already carrying them).
        inspector = sa_inspect(engine)
        months_missing = inspector.has_table(ConferenceRow.__tablename__) and (
            "conference_month"
            not in {c["name"] for c in inspector.get_columns(ConferenceRow.__tablename__)}
        )
        _ensure_columns(engine)
        # Cache before any backfill helper, which calls get_engine reentrantly.
        _ENGINES[db_url] = engine
        if migrated:
            # The freshly added ``category`` column is empty after a rename; derive
            # it from the (renamed) subcategory tags so search/sort work at once.
            recompute_categories(db_url)
        if months_missing:
            # The freshly added month columns are empty; derive them from the
            # existing dates so they match what the table shows immediately.
            recompute_months(db_url)
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
            # Subcategory floor: a seeded series' tags are curated and
            # authoritative, so a discovery run cannot overwrite them with model
            # free-text. The derived category is re-applied to match.
            seed_subs = seed_subcategories_for(conf.acronym)
            if seed_subs:
                row.subcategory = ", ".join(seed_subs)
                row.category = ", ".join(categories_for_subcategories(seed_subs)) or None
            # Format floor: some conferences have hardcoded formats that override
            # discovery, ensuring consistency across refreshes.
            hardcoded_fmts = HARDCODED_FORMATS.get(conf.acronym.upper())
            if hardcoded_fmts:
                row.format = ", ".join(hardcoded_fmts)
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
# ``subcategory`` is handled separately (it may arrive as a list or a delimited
# string and is normalized to the comma-joined form, with ``category`` derived
# from it), so it is not in this tuple.
_MERGEABLE_TEXT_FIELDS = (
    "name",
    "location",
    "url",
    "cost",
    "attendance_source",
    "notes",
    "prior_registration",
    "upcoming_registration",
)

# Integer fields a researched record may carry. Parsed from int/numeric strings;
# invalid values are silently ignored. ``size`` is not mergeable -- it is derived
# from ``attendance`` after the merge (see ``merge_records``).
_MERGEABLE_INT_FIELDS = ("attendance", "attendance_year")


def _record_subcategory(record: dict):
    """The granular tag value from a record, checking the accepted keys in order.

    Prefers ``subcategory`` / ``subcategories``; falls back to the legacy
    ``category`` / ``categories`` keys so older CSV/JSON exports still ingest.
    Returns the raw value (list or string), or ``None`` when none is present.
    """
    for key in ("subcategory", "subcategories", "category", "categories"):
        value = record.get(key)
        if value not in (None, "", []):
            return value
    return None


def merge_records(
    records: Iterable[dict],
    db_url: str = DEFAULT_DATABASE_URL,
) -> int:
    """Merge partial researched records into existing rows without clobbering.

    Each record is a plain dict keyed by ``id`` (the upper-cased acronym). Only
    keys that are present *and* non-null/non-empty overwrite the stored value, so
    a record that carries just newly found dates leaves the row's name, url,
    subcategory, and attendance untouched. This is the offline counterpart to
    discovery: research gathered by any means (e.g. an interactive agent's web
    search) can be folded into the table without calling the Anthropic API.

    Date fields accept ISO ``YYYY-MM-DD`` strings; ``remote_option`` is validated
    against its enum and silently ignored if invalid; ``attendance`` /
    ``attendance_year`` are parsed as integers. The ``size`` bucket and the broad
    ``category`` are never taken from a record -- size is recomputed from
    ``attendance`` and category is derived from ``subcategory`` after merging, so
    neither can disagree with what it is computed from. The granular tag arrives
    under ``subcategory`` / ``subcategories`` (the legacy ``category`` /
    ``categories`` keys are still accepted as aliases). Records whose id matches no
    existing row are inserted only when they also supply ``name`` and a
    subcategory (otherwise skipped). Returns the number of rows written.
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
                if not (record.get("name") and _record_subcategory(record)):
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
            for field in _MERGEABLE_INT_FIELDS:
                value = record.get(field)
                if value not in (None, ""):
                    try:
                        setattr(row, field, int(str(value).strip()))
                        changed = True
                    except ValueError:
                        pass
            # Subcategory may be a list or a delimited string; store the
            # normalized, comma-joined form so multi-tag records merge cleanly.
            subcategory = _record_subcategory(record)
            if subcategory is not None:
                subs = normalize_subcategories(subcategory)
                if subs:
                    row.subcategory = ", ".join(subs)
                    changed = True
            # Formats (abstract/paper/poster/oral): a list or a delimited string,
            # normalized to the canonical-ordered, comma-joined form. Accepts the
            # singular ``format`` key or the plural ``formats``. Only a non-empty
            # normalized value overwrites, so a partial record never clears it.
            fmt_value = record.get("format")
            if fmt_value in (None, "", []):
                fmt_value = record.get("formats")
            if fmt_value not in (None, "", []):
                fmts = normalize_formats(fmt_value)
                joined = ", ".join(fmts)
                if joined and row.format != joined:
                    row.format = joined
                    changed = True
            remote = record.get("remote_option")
            if remote not in (None, ""):
                try:
                    row.remote_option = RemoteOption(str(remote).strip().lower()).value
                    changed = True
                except ValueError:
                    pass
            # Size is always derived from the (possibly just-merged) attendance,
            # never taken from the record, so the stored bucket can't disagree with
            # the figure. Recompute it whenever it would change.
            size = size_for_attendance(row.attendance)
            size_value = size.value if size else None
            if size_value != row.size:
                row.size = size_value
                changed = True
            # Flagship link floor: a curated deep link wins over any URL a refresh
            # merged in, mirroring upsert_conferences so neither write path can
            # regress a verified link to a weaker homepage.
            floor = curated_seed_url(acronym)
            if floor and row.url != floor:
                row.url = floor
                changed = True
            # Subcategory floor: a seeded series' tags are curated and
            # authoritative, so they win over any tag the record carried (mirrors
            # the url floor and upsert_conferences). Non-seed rows keep their own.
            seed_subs = seed_subcategories_for(acronym)
            if seed_subs:
                joined = ", ".join(seed_subs)
                if row.subcategory != joined:
                    row.subcategory = joined
                    changed = True
            # Category is always derived from the (possibly just-merged) subcategory,
            # never taken from the record, so the stored bucket can't disagree with
            # the tags. Recompute it whenever it would change.
            category = ", ".join(categories_for_subcategories(normalize_subcategories(row.subcategory))) or None
            if category != row.category:
                row.category = category
                changed = True
            # Format floor: some conferences have hardcoded formats that override
            # merge/discovery, ensuring consistency across refreshes.
            hardcoded_fmts = HARDCODED_FORMATS.get(row_id)
            if hardcoded_fmts:
                hardcoded_format_str = ", ".join(hardcoded_fmts)
                if row.format != hardcoded_format_str:
                    row.format = hardcoded_format_str
                    changed = True
            if changed:
                written += 1
        session.commit()
    return written


def seed_conferences(db_url: str = DEFAULT_DATABASE_URL, overwrite: bool = False) -> int:
    """Populate the table from the static seed catalog (``config.SEED_CONFERENCES``).

    Builds a minimal :class:`Conference` for every seed -- acronym, name,
    subcategory, and the official URL -- leaving the deadline/date and
    attendance/size fields empty. This makes the table usable without the discovery
    API; a later discovery run fills the dates and attendance into the same rows in
    place.

    Only *missing* rows are inserted by default, so seeding never clobbers data
    already discovered; pass ``overwrite=True`` to also refresh existing rows'
    seed-derived fields. Idempotent. Returns the number of rows written.
    """
    engine = get_engine(db_url)
    written = 0
    with Session(engine) as session:
        for acronym, name, subcategory in SEED_CONFERENCES:
            conf = Conference(
                acronym=acronym,
                name=name,
                subcategory=subcategory,
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


def distinct_subcategories(db_url: str = DEFAULT_DATABASE_URL) -> set[str]:
    """Return the set of subcategory tags currently present in the table.

    Tags are normalized (lowercased, split on ``,``/``;``) the same way the model
    stores them, so callers can compare a candidate tag against the table's
    existing vocabulary -- e.g. to warn when a manual ``add`` introduces a tag no
    other row uses.
    """
    engine = get_engine(db_url)
    subs: set[str] = set()
    with Session(engine) as session:
        for joined in session.scalars(select(ConferenceRow.subcategory)):
            subs.update(normalize_subcategories(joined))
    return subs


def recompute_sizes(db_url: str = DEFAULT_DATABASE_URL) -> int:
    """Re-derive every row's stored ``size`` from its ``attendance``.

    ``size`` is denormalized (stored so the search/sort can use it as a column) but
    only ever written from :func:`models.size_for_attendance` on insert/merge. If
    the size *thresholds* change, already-stored rows keep their old bucket until
    rewritten -- this re-derives them all in place. Idempotent. Returns the number
    of rows whose stored size changed.
    """
    engine = get_engine(db_url)
    changed = 0
    with Session(engine) as session:
        for row in session.scalars(select(ConferenceRow)):
            size = size_for_attendance(row.attendance)
            value = size.value if size else None
            if value != row.size:
                row.size = value
                changed += 1
        session.commit()
    return changed


def recompute_categories(db_url: str = DEFAULT_DATABASE_URL) -> int:
    """Re-derive every row's stored ``category`` from its ``subcategory``.

    ``category`` is denormalized (stored so the search/sort can use it as a column)
    but only ever written from :func:`models.categories_for_subcategories` on
    insert/merge. If the subcategory->category *map* changes, already-stored rows
    keep their old bucket until rewritten -- this re-derives them all in place.
    Idempotent. Returns the number of rows whose stored category changed.
    """
    engine = get_engine(db_url)
    changed = 0
    with Session(engine) as session:
        for row in session.scalars(select(ConferenceRow)):
            category = ", ".join(
                categories_for_subcategories(normalize_subcategories(row.subcategory))
            ) or None
            if category != row.category:
                row.category = category
                changed += 1
        session.commit()
    return changed


def recompute_months(db_url: str = DEFAULT_DATABASE_URL) -> int:
    """Re-derive every row's stored month fields from its dates.

    ``conference_month`` / ``abstract_month`` / ``paper_month`` are denormalized
    (stored so the search/sort can use them as columns) but only ever written from
    the ``Conference`` model's ``*_month`` properties on insert/merge -- each is the
    month of the upcoming date, falling back to the prior one. This re-derives them
    all in place (e.g. to backfill rows stored before the columns existed, or after
    an out-of-band date edit). Idempotent. Returns the number of rows whose stored
    months changed.
    """
    engine = get_engine(db_url)
    changed = 0
    with Session(engine) as session:
        for row in session.scalars(select(ConferenceRow)):
            conf = _row_to_model(row)
            new = (conf.conference_month, conf.abstract_month, conf.paper_month)
            if new != (row.conference_month, row.abstract_month, row.paper_month):
                row.conference_month, row.abstract_month, row.paper_month = new
                changed += 1
        session.commit()
    return changed


def known_attendance_sources(
    db_url: str = DEFAULT_DATABASE_URL,
    subcategories: "Optional[Iterable[str]]" = None,
) -> dict:
    """Map acronym id -> ``{"source": url, "year": int|None}`` for rows that carry a
    stored attendance source, optionally restricted to the given subcategories.

    Discovery feeds this back into the research prompt on a refresh so the model
    re-checks the URL a figure last came from (and, when the URL is year-stamped,
    the next edition's URL) before searching the web afresh. The map is built from
    the live table, so it always reflects the most recently found source per row --
    no separate static list to maintain.
    """
    subs = list(subcategories) if subcategories else None
    rows: List[Conference] = []
    if subs:
        for sub in subs:
            rows.extend(query_conferences(subcategory=sub, db_url=db_url))
    else:
        rows = query_conferences(db_url=db_url)
    out: dict = {}
    for c in rows:
        if c.attendance_source and c.id not in out:
            out[c.id] = {"source": c.attendance_source, "year": c.attendance_year}
    return out


def query_conferences(
    subcategory: Optional[str] = None,
    category: Optional[str] = None,
    size: Optional[str] = None,
    db_url: str = DEFAULT_DATABASE_URL,
) -> List[Conference]:
    """Return stored conferences, optionally filtered by subcategory/category/size.

    ``subcategory`` filters the granular tag column; ``category`` filters the broad
    derived bucket. Both use a substring match since a row may list several tags.
    """
    engine = get_engine(db_url)
    stmt = select(ConferenceRow)
    if subcategory is not None:
        # Substring match: a row's subcategory column may list several tags
        # (e.g. "radiology, pediatrics"), so an exact match would miss it.
        stmt = stmt.where(ConferenceRow.subcategory.ilike(f"%{subcategory}%"))
    if category is not None:
        stmt = stmt.where(ConferenceRow.category.ilike(f"%{category}%"))
    if size is not None:
        stmt = stmt.where(ConferenceRow.size == size)
    stmt = stmt.order_by(ConferenceRow.upcoming_start_date.is_(None), ConferenceRow.upcoming_start_date)
    with Session(engine) as session:
        return [_row_to_model(row) for row in session.scalars(stmt)]
