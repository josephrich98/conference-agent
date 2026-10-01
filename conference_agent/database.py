"""SQLAlchemy persistence for conference records.

Provides the ORM model, engine wiring, and idempotent upsert/query helpers. The
same ORM runs against SQLite (local) or any SQLAlchemy backend with only a
connection-string change.

Idempotency: rows are keyed on ``Conference.id`` (the slug of the name), so
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
    seed_acronym_for_name,
    seed_subcategories,
    seed_subcategories_for,
)
from conference_agent.deadline_time import (
    KINDS,
    normalize_time,
    normalize_timezone,
    parse_legacy_deadline_time,
)
from conference_agent.models import (
    Conference,
    RemoteOption,
    categories_for_subcategories,
    name_id,
    normalize_formats,
    normalize_subcategories,
    size_for_attendance,
)


class Base(DeclarativeBase):
    pass


class ConferenceRow(Base):
    """ORM mapping of one conference series (see :class:`Conference`)."""

    __tablename__ = "conferences"

    # Natural primary key: the slug of the name (Conference.id / models.name_id).
    # Series are indexed by name; two may share an acronym.
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
    # Second, later abstract deadline of the edition -- a poster-only deadline
    # when the main one is talk-only, or a late-breaking / late-poster round.
    # See ``Conference.upcoming_late_abstract_deadline``. NULL for the majority
    # of series, which publish a single abstract deadline.
    prior_late_abstract_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    prior_paper_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    prior_start_date: Mapped[Optional[Date]] = mapped_column(Date)
    prior_end_date: Mapped[Optional[Date]] = mapped_column(Date)
    # Registration is free text (windows like "Early bird: ...; Regular: ..."),
    # not a date -- a meeting may publish several windows, only an opening date, or
    # nothing at all, so a single Date column cannot represent it. Stored as text;
    # there is consequently no derived registration month.
    prior_registration: Mapped[Optional[str]] = mapped_column(Text)

    upcoming_abstract_deadline: Mapped[Optional[Date]] = mapped_column(Date)
    upcoming_late_abstract_deadline: Mapped[Optional[Date]] = mapped_column(Date)
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
    # Time of day (24-hour HH:MM) and zone each deadline kind closes at -- the
    # stored source of truth (see ``Conference.abstract_time`` etc.). Per series,
    # not per edition; the deadline columns themselves stay pure dates.
    abstract_time: Mapped[Optional[str]] = mapped_column(String)
    abstract_timezone: Mapped[Optional[str]] = mapped_column(String)
    late_abstract_time: Mapped[Optional[str]] = mapped_column(String)
    late_abstract_timezone: Mapped[Optional[str]] = mapped_column(String)
    paper_time: Mapped[Optional[str]] = mapped_column(String)
    paper_timezone: Mapped[Optional[str]] = mapped_column(String)
    # The user-facing "Deadline time" text, *derived* from the six columns above
    # (``Conference.deadline_time``) -- like ``size`` / ``category`` it is stored
    # denormalized so search and the table can use it as a column, and is only ever
    # written by the derivation, never accepted as input.
    deadline_time: Mapped[Optional[str]] = mapped_column(Text)
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
    # Page-watch state (``refresh.run_watch``), also row-level scheduling state:
    # the fingerprint of the dates on the row's official pages as of the last
    # agent run (or first observation), and the date those pages were last
    # fetched. A changed fingerprint is what triggers an agent run.
    watch_fingerprint: Mapped[Optional[str]] = mapped_column(String)
    watch_checked: Mapped[Optional[Date]] = mapped_column(Date)
    # The link the stored fingerprint was taken from. A refresh may replace the
    # official link (e.g. with a year-specific site), and a fingerprint of the
    # old page says nothing about the new one, so a changed link re-baselines
    # instead of counting as a change.
    watch_url: Mapped[Optional[str]] = mapped_column(Text)

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
    late_abstract_month: Mapped[Optional[int]] = mapped_column(Integer)
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
late_abstract_date_expr = func.coalesce(
    ConferenceRow.upcoming_late_abstract_deadline,
    ConferenceRow.prior_late_abstract_deadline,
)
paper_date_expr = func.coalesce(
    ConferenceRow.upcoming_paper_deadline, ConferenceRow.prior_paper_deadline
)
# Registration is free text (see ``ConferenceRow.prior_registration``), so unlike
# the deadline/date fields it has no coalesced date expression and no derived
# month column.

# The stored derived-month columns. ``get_engine`` backfills them when a
# pre-existing database is missing any one of them (see ``recompute_months``).
_MONTH_COLUMNS = frozenset(
    {"conference_month", "abstract_month", "late_abstract_month", "paper_month"}
)


class IdAliasRow(Base):
    """A former id of a series and the id it moved to.

    Ids are derived from names, so an id changes when a series is renamed (and
    every id changed once, when rows moved from acronym ids to name ids). Calendar
    UIDs, stored email subscriptions, the subscriber snapshot, and published
    ``/c/<id>/`` URLs may still carry a former id; :func:`resolve_id` follows
    these rows (possibly a chain) to the current one.
    """

    __tablename__ = "id_aliases"

    old_id: Mapped[str] = mapped_column(String, primary_key=True)
    new_id: Mapped[str] = mapped_column(String, nullable=False)


# --- Conversion helpers ----------------------------------------------------

_DATE_FIELDS = (
    "prior_abstract_deadline",
    "prior_late_abstract_deadline",
    "prior_paper_deadline",
    "prior_start_date",
    "prior_end_date",
    "upcoming_abstract_deadline",
    "upcoming_late_abstract_deadline",
    "upcoming_paper_deadline",
    "upcoming_start_date",
    "upcoming_end_date",
)
# The six structured deadline-time columns (a time and a zone per deadline kind).
# ``deadline_time`` is derived from them, so it is not in this tuple.
_TIME_FIELDS = tuple(f"{k}_{part}" for k in KINDS for part in ("time", "timezone"))
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
    *_TIME_FIELDS,
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
    for field in _TIME_FIELDS:
        data[field] = getattr(row, field)
    return Conference(**data)


def _derived_months(row: ConferenceRow) -> tuple:
    """The row's four derived month values, in column order.

    Derives them through the :class:`Conference` model's ``*_month`` properties
    so the rule lives in exactly one place (each is the month of the upcoming
    date, falling back to the prior). Returns
    ``(conference, abstract, late_abstract, paper)``.
    """
    conf = _row_to_model(row)
    return (
        conf.conference_month,
        conf.abstract_month,
        conf.late_abstract_month,
        conf.paper_month,
    )


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
    # The display text is derived from the structured times (and which deadline
    # dates exist), never taken as input -- so it always matches.
    row.deadline_time = conf.deadline_time
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
    row.late_abstract_month = conf.late_abstract_month
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


def _migrate_ids_to_names(engine: Engine) -> int:
    """Re-key every row whose id is not the slug of its name; returns the count.

    Rows were once keyed by the upper-cased acronym. Each moved row records its
    former id in ``id_aliases``. Two rows whose names share a slug cannot both
    move, so that raises rather than merging them silently.
    """
    with Session(engine) as session:
        rows = list(session.scalars(select(ConferenceRow)))
        targets: dict[str, str] = {}
        for row in rows:
            new_id = name_id(row.name)
            if not new_id:
                raise RuntimeError(f"conference {row.id} has a name with no id: {row.name!r}")
            if new_id in targets:
                raise RuntimeError(
                    f"conferences {targets[new_id]} and {row.id} have the same name "
                    f"({row.name!r}); rename or delete one before opening the database"
                )
            targets[new_id] = row.id
        moves = [(row.id, name_id(row.name)) for row in rows if row.id != name_id(row.name)]
        if not moves:
            return 0
        table = ConferenceRow.__tablename__
        # Two passes through a temporary id, so a move onto an id another row is
        # leaving in the same migration cannot collide.
        for old_id, _ in moves:
            session.execute(
                text(f"UPDATE {table} SET id = :tmp WHERE id = :old"),  # nosec B608
                {"tmp": f"~migrating~{old_id}", "old": old_id},
            )
        for old_id, new_id in moves:
            session.execute(
                text(f"UPDATE {table} SET id = :new WHERE id = :tmp"),  # nosec B608
                {"new": new_id, "tmp": f"~migrating~{old_id}"},
            )
            session.merge(IdAliasRow(old_id=old_id, new_id=new_id))
        session.commit()
    return len(moves)


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
        # Any missing month column means the backfill is needed: a database
        # created before ``late_abstract_month`` was added has the other three but
        # not it, and ``_ensure_columns`` adds columns empty.
        months_missing = inspector.has_table(ConferenceRow.__tablename__) and not (
            _MONTH_COLUMNS
            <= {c["name"] for c in inspector.get_columns(ConferenceRow.__tablename__)}
        )
        # Likewise detect a database predating the structured deadline-time
        # columns, whose free-text ``deadline_time`` must be parsed into them.
        times_missing = inspector.has_table(ConferenceRow.__tablename__) and not (
            set(_TIME_FIELDS)
            <= {c["name"] for c in inspector.get_columns(ConferenceRow.__tablename__)}
        )
        _ensure_columns(engine)
        # Move rows keyed by the legacy acronym id onto their name id.
        _migrate_ids_to_names(engine)
        # Cache before any backfill helper, which calls get_engine reentrantly.
        _ENGINES[db_url] = engine
        if migrated:
            # The freshly added ``category`` column is empty after a rename; derive
            # it from the (renamed) subcategory tags so search/sort work at once.
            recompute_categories(db_url)
        if times_missing:
            # Parse the legacy free-text deadline times into the new columns.
            backfill_deadline_times(db_url)
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
            row = match_row(session, conf.name, conf.acronym, conf.subcategories)
            if row is None:
                row = ConferenceRow(id=conf.id, name=conf.name)
                session.add(row)
            # The stored name is the index: a run that reports it under another
            # spelling updates the row but never renames it (see ``match_row``).
            name = row.name
            _apply_model_to_row(row, conf)
            row.name = name
            seed = seed_acronym_for_name(name)
            floor = curated_seed_url(seed)
            if floor:
                row.url = floor
            # Subcategory floor: a seeded series' tags are curated and
            # authoritative, so a discovery run cannot overwrite them with model
            # free-text. The derived category is re-applied to match.
            seed_subs = seed_subcategories_for(seed)
            if seed_subs:
                row.subcategory = ", ".join(seed_subs)
                row.category = ", ".join(categories_for_subcategories(seed_subs)) or None
            # Format floor: some conferences have hardcoded formats that override
            # discovery, ensuring consistency across refreshes.
            hardcoded_fmts = HARDCODED_FORMATS.get((seed or "").upper())
            if hardcoded_fmts:
                row.format = ", ".join(hardcoded_fmts)
            session.flush()
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


def _dated_kinds(row: "ConferenceRow") -> List[str]:
    """The deadline kinds a row has any date for (prior or upcoming)."""
    return [
        k
        for k in KINDS
        if getattr(row, f"upcoming_{k}_deadline") or getattr(row, f"prior_{k}_deadline")
    ]


def merge_records(
    records: Iterable[dict],
    db_url: str = DEFAULT_DATABASE_URL,
) -> int:
    """Merge partial researched records into existing rows without clobbering.

    Each record names its row by ``id`` (exactly, or a former id via
    ``id_aliases``) or else by ``name`` / ``acronym`` (see :func:`match_row`). Only
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
    ``categories`` keys are still accepted as aliases). A record that matches no
    existing row is inserted (under the id of its name) only when it supplies a
    ``name`` and a subcategory (otherwise skipped). An existing row's name is never changed here -- it is the index;
    see :func:`rename_conference`. Returns the number of rows written.
    """
    engine = get_engine(db_url)
    written = 0
    with Session(engine) as session:
        for record in records:
            explicit_id = str(record.get("id") or "").strip()
            name = " ".join(str(record.get("name") or "").split())
            acronym = str(record.get("acronym") or "").strip()
            if explicit_id:
                row = resolve_row(session, explicit_id)
            elif name:
                row = match_row(
                    session, name, acronym,
                    normalize_subcategories(_record_subcategory(record) or ""),
                )
            else:
                continue
            if row is None:
                if not (name and name_id(name) and _record_subcategory(record)):
                    continue
                row = ConferenceRow(id=name_id(name), acronym=acronym or name, name=name)
                session.add(row)

            changed = False
            for field in _DATE_FIELDS:
                if field in record and record[field] not in (None, ""):
                    setattr(row, field, _coerce_date(record[field]))
                    changed = True
            for field in _MERGEABLE_TEXT_FIELDS:
                if field == "name":
                    continue  # the index; set on insert above, never merged
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
            # Deadline times: six structured fields (a time and a zone per kind),
            # normalized to 24-hour HH:MM and canonical zone codes; an unreadable
            # value is ignored. The legacy free-text ``deadline_time`` is still
            # accepted (older exports) and fills whichever structured fields the
            # record did not set itself.
            time_values = {f: record.get(f) for f in _TIME_FIELDS}
            legacy = record.get("deadline_time")
            if legacy not in (None, ""):
                dated = _dated_kinds(row)
                for key, value in parse_legacy_deadline_time(str(legacy), dated).items():
                    if time_values.get(key) in (None, ""):
                        time_values[key] = value
            for field, value in time_values.items():
                if value in (None, ""):
                    continue
                norm = (normalize_time if field.endswith("_time") else normalize_timezone)(value)
                if norm and getattr(row, field) != norm:
                    setattr(row, field, norm)
                    changed = True
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
            seed = seed_acronym_for_name(row.name)
            floor = curated_seed_url(seed)
            if floor and row.url != floor:
                row.url = floor
                changed = True
            # Subcategory floor: a seeded series' tags are curated and
            # authoritative, so they win over any tag the record carried (mirrors
            # the url floor and upsert_conferences). Non-seed rows keep their own.
            seed_subs = seed_subcategories_for(seed)
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
            hardcoded_fmts = HARDCODED_FORMATS.get((seed or "").upper())
            if hardcoded_fmts:
                hardcoded_format_str = ", ".join(hardcoded_fmts)
                if row.format != hardcoded_format_str:
                    row.format = hardcoded_format_str
                    changed = True
            # The month columns are derived from the (possibly just-merged) dates,
            # never taken from the record -- so, like size and category above, they
            # are re-derived here rather than left carrying the month of a date the
            # merge has since replaced.
            # The display text is derived from the structured times and the dates,
            # so re-derive it after both may have changed.
            deadline_text = _row_to_model(row).deadline_time
            if deadline_text != row.deadline_time:
                row.deadline_time = deadline_text
                changed = True
            months = _derived_months(row)
            if months != (
                row.conference_month,
                row.abstract_month,
                row.late_abstract_month,
                row.paper_month,
            ):
                (
                    row.conference_month,
                    row.abstract_month,
                    row.late_abstract_month,
                    row.paper_month,
                ) = months
                changed = True
            # Flush so a later record in the batch can match this row.
            session.flush()
            if changed:
                written += 1
        session.commit()
    return written


# Per-edition fields, as the suffix after ``prior_`` / ``upcoming_``.
_EDITION_SUFFIXES = (
    "abstract_deadline",
    "late_abstract_deadline",
    "paper_deadline",
    "start_date",
    "end_date",
    "registration",
)
# Two start dates further apart than this belong to different editions.
NEW_EDITION_GAP_DAYS = 180


def _roll_editions(row: ConferenceRow, conf: Conference) -> None:
    """Realign a row's edition slots with a refreshed record before merging.

    A fill-only merge cannot clear a field, so when a refresh reports a new
    edition, fields of the old edition that the new record leaves blank would
    otherwise survive under the wrong edition. When the refreshed upcoming
    edition starts well after the stored one, the stored upcoming edition has
    become the prior one: shift it into the prior slots and clear the upcoming
    slots. When the refreshed prior edition is a different edition from the
    stored prior one, clear the stored prior slots.
    """
    old_up, new_up = row.upcoming_start_date, conf.upcoming_start_date
    if old_up and new_up and (new_up - old_up).days > NEW_EDITION_GAP_DAYS:
        for suffix in _EDITION_SUFFIXES:
            setattr(row, f"prior_{suffix}", getattr(row, f"upcoming_{suffix}"))
            setattr(row, f"upcoming_{suffix}", None)
    old_prior, new_prior = row.prior_start_date, conf.prior_start_date
    if old_prior and new_prior and abs((new_prior - old_prior).days) > NEW_EDITION_GAP_DAYS:
        for suffix in _EDITION_SUFFIXES:
            setattr(row, f"prior_{suffix}", None)


def _conference_to_record(conf: Conference) -> dict:
    """A :class:`Conference` as a :func:`merge_records` record (blank fields omitted)."""
    record: dict = {"id": conf.id, "acronym": conf.acronym, "name": conf.name}
    for field in (*_DATE_FIELDS, *_MERGEABLE_TEXT_FIELDS, *_MERGEABLE_INT_FIELDS, *_TIME_FIELDS):
        value = getattr(conf, field)
        if value not in (None, ""):
            record[field] = value
    if conf.subcategories:
        record["subcategories"] = conf.subcategories
    if conf.formats:
        record["formats"] = conf.formats
    if conf.remote_option is not None:
        record["remote_option"] = conf.remote_option.value
    return record


def apply_refreshed_conferences(
    conferences: Iterable[Conference], db_url: str = DEFAULT_DATABASE_URL
) -> int:
    """Fold a targeted refresh into *existing* rows without losing data.

    A targeted re-check (``discover.refresh_conferences``) researches a handful
    of named series and may not re-find every field (attendance, cost, ...), so
    unlike :func:`upsert_conferences` it must not overwrite a stored value with a
    blank. Each record is merged fill-only via :func:`merge_records`, after
    :func:`_roll_editions` realigns the edition slots when the refresh reports a
    new edition. Records for series not already in the table are skipped.
    Returns the number of rows written.
    """
    conferences = list(conferences)
    engine = get_engine(db_url)
    records = []
    with Session(engine) as session:
        for conf in conferences:
            row = match_row(session, conf.name, conf.acronym, conf.subcategories)
            if row is None:
                continue
            _roll_editions(row, conf)
            record = _conference_to_record(conf)
            record["id"] = row.id
            records.append(record)
        session.commit()
    return merge_records(records, db_url=db_url)


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
            row = match_row(session, conf.name, conf.acronym, conf.subcategories)
            if row is None:
                row = ConferenceRow(id=conf.id, name=conf.name)
                session.add(row)
            elif not overwrite:
                continue
            name = row.name
            _apply_model_to_row(row, conf)
            row.name = name
            session.flush()
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


def discovery_subcategories(db_url: str = DEFAULT_DATABASE_URL) -> list[str]:
    """Every field a whole-table discovery run surveys, sorted.

    The subcategories present in the table, plus the seed fields (so a field
    whose rows were all deleted, or a fresh database, is still covered).
    """
    return sorted(distinct_subcategories(db_url) | set(seed_subcategories()))


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
            new = _derived_months(row)
            current = (
                row.conference_month,
                row.abstract_month,
                row.late_abstract_month,
                row.paper_month,
            )
            if new != current:
                (
                    row.conference_month,
                    row.abstract_month,
                    row.late_abstract_month,
                    row.paper_month,
                ) = new
                changed += 1
        session.commit()
    return changed


def backfill_deadline_times(db_url: str = DEFAULT_DATABASE_URL) -> int:
    """Parse legacy free-text ``deadline_time`` into the structured columns.

    One-time migration, run when a database predating the six time/zone columns is
    first opened. Only rows with a stored ``deadline_time`` and no structured value
    yet are touched, so it is idempotent and never overwrites a structured value.
    An unlabeled time is assigned to each deadline kind the row has a date for. A
    text that parses to nothing (e.g. a bare "EOD") is left as it was. The display
    text is then re-derived, which also normalizes it ("11:59 PM EST" ->
    "11:59 PM ET"). Returns the number of rows converted.
    """
    engine = get_engine(db_url)
    converted = 0
    with Session(engine) as session:
        for row in session.scalars(select(ConferenceRow)):
            if not row.deadline_time or any(getattr(row, f) for f in _TIME_FIELDS):
                continue
            parsed = parse_legacy_deadline_time(row.deadline_time, _dated_kinds(row))
            if not parsed:
                continue
            for field, value in parsed.items():
                setattr(row, field, value)
            row.deadline_time = _row_to_model(row).deadline_time
            converted += 1
        session.commit()
    return converted


def recompute_deadline_times(db_url: str = DEFAULT_DATABASE_URL) -> int:
    """Re-derive every row's stored ``deadline_time`` text from its structured times.

    The text depends on the six time/zone columns and on which deadline dates the
    row has, so an out-of-band edit to either leaves it stale until rewritten.
    Idempotent. Returns the number of rows whose stored text changed.
    """
    engine = get_engine(db_url)
    changed = 0
    with Session(engine) as session:
        for row in session.scalars(select(ConferenceRow)):
            text_now = _row_to_model(row).deadline_time
            if text_now != row.deadline_time:
                row.deadline_time = text_now
                changed += 1
        session.commit()
    return changed


def attendance_hint_key(conf: Conference) -> str:
    """The key :func:`known_attendance_sources` files a series' hint under."""
    return f"{conf.acronym} — {conf.name}" if conf.acronym != conf.name else conf.name


def attendance_hints_for(targets: Iterable[Conference], hints: dict) -> dict:
    """The entries of *hints* (from :func:`known_attendance_sources`) for *targets*."""
    keys = {attendance_hint_key(t) for t in targets}
    return {k: v for k, v in hints.items() if k in keys}


def known_attendance_sources(
    db_url: str = DEFAULT_DATABASE_URL,
    subcategories: "Optional[Iterable[str]]" = None,
) -> dict:
    """Map ``"ACRONYM — Name"`` -> ``{"source": url, "year": int|None}`` for rows that carry a
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
        label = attendance_hint_key(c)
        if c.attendance_source and label not in out:
            out[label] = {"source": c.attendance_source, "year": c.attendance_year}
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


def delete_conferences(ids: Iterable[str], db_url: str = DEFAULT_DATABASE_URL) -> int:
    """Delete the rows with the given ids; returns the number removed.

    Only the manual ``conference-agent delete`` path calls this -- retired series
    are otherwise never deleted (see ``refresh.is_retired``). A deleted seed series
    returns on the next ``seed`` run, and discovery may find any series again.
    """
    engine = get_engine(db_url)
    removed = 0
    with Session(engine) as session:
        for row_id in ids:
            row = session.get(ConferenceRow, row_id)
            if row is not None:
                session.delete(row)
                removed += 1
        session.commit()
    return removed


def resolve_row(session: Session, row_id: str) -> "ConferenceRow | None":
    """The row with id *row_id*, following ``id_aliases`` from a former id."""
    seen: set[str] = set()
    while row_id and row_id not in seen:
        row = session.get(ConferenceRow, row_id)
        if row is not None:
            return row
        seen.add(row_id)
        alias = session.get(IdAliasRow, row_id)
        row_id = alias.new_id if alias else None
    return None


def match_row(
    session: Session,
    name: str,
    acronym: "str | None" = None,
    subcategories: "Iterable[str] | None" = None,
) -> "ConferenceRow | None":
    """The stored series a discovered or researched conference refers to.

    Names are the index, so a name match (by id, or a former id after a rename)
    wins. Discovery does not always reproduce a stored name verbatim (it has
    reported "RECOMB" both as "Research in Computational Molecular Biology" and
    with an "International Conference on" prefix), so with no name match a
    record still updates the one row with the same acronym *and* an overlapping
    subcategory. A same-acronym series in an unrelated field matches nothing
    and becomes its own row.
    """
    row = resolve_row(session, name_id(name)) if name else None
    if row is not None or not acronym:
        return row
    wanted = set(subcategories or ())
    if not wanted:
        return None
    candidates = [
        r
        for r in session.scalars(
            select(ConferenceRow).where(func.upper(ConferenceRow.acronym) == acronym.strip().upper())
        )
        if wanted & set(normalize_subcategories(r.subcategory or ""))
    ]
    return candidates[0] if len(candidates) == 1 else None


def release_ids(ids: Iterable[str], db_url: str = DEFAULT_DATABASE_URL) -> None:
    """Forget that *ids* were ever former ids, so new series can take them.

    Adding a series under a name a renamed series used to have makes that name's
    id current again; without this, the alias would route it to the renamed row.
    """
    engine = get_engine(db_url)
    with Session(engine) as session:
        for row_id in ids:
            alias = session.get(IdAliasRow, row_id)
            if alias is not None:
                session.delete(alias)
        session.commit()


def resolve_ids(ids: Iterable[str], db_url: str = DEFAULT_DATABASE_URL) -> dict:
    """Map each id (current or former) to the current id, dropping unknown ones."""
    engine = get_engine(db_url)
    out: dict = {}
    with Session(engine) as session:
        for row_id in ids:
            row = resolve_row(session, row_id)
            if row is not None:
                out[row_id] = row.id
    return out


def former_ids(db_url: str = DEFAULT_DATABASE_URL) -> dict:
    """Every former id that still leads to a row, mapped to that row's current id."""
    engine = get_engine(db_url)
    with Session(engine) as session:
        old_ids = list(session.scalars(select(IdAliasRow.old_id)))
    return resolve_ids(old_ids, db_url=db_url)


def set_acronym(row_id: str, acronym: str, db_url: str = DEFAULT_DATABASE_URL) -> None:
    """Change a series' acronym. The id follows the name, so it is unaffected."""
    engine = get_engine(db_url)
    with Session(engine) as session:
        row = session.get(ConferenceRow, row_id)
        if row is None:
            raise ValueError(f"no conference with id {row_id}")
        row.acronym = acronym.strip()
        session.commit()


def rename_conference(row_id: str, new_name: str, db_url: str = DEFAULT_DATABASE_URL) -> str:
    """Rename a series, moving it to its new name's id; returns the new id.

    The former id is kept in ``id_aliases`` so links, calendar UIDs, and
    subscriptions that carry it still resolve. Raises ``ValueError`` when the new
    name is empty or already names another series.
    """
    new_name = " ".join(new_name.split())
    new_id = name_id(new_name)
    if not new_id:
        raise ValueError("the new name must contain at least one ASCII letter or digit")
    engine = get_engine(db_url)
    with Session(engine) as session:
        row = session.get(ConferenceRow, row_id)
        if row is None:
            raise ValueError(f"no conference with id {row_id}")
        if new_id != row_id:
            if session.get(ConferenceRow, new_id) is not None:
                raise ValueError(f"'{new_name}' already names another conference")
            session.execute(
                text(f"UPDATE {ConferenceRow.__tablename__} SET id = :new, name = :name WHERE id = :old"),  # nosec B608
                {"new": new_id, "name": new_name, "old": row_id},
            )
            session.merge(IdAliasRow(old_id=row_id, new_id=new_id))
            # The new id is current again if it was ever a former one.
            alias = session.get(IdAliasRow, new_id)
            if alias is not None:
                session.delete(alias)
        else:
            row.name = new_name
        session.commit()
    return new_id
