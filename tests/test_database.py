"""Offline tests for the SQLAlchemy persistence layer.

Use a temporary SQLite file so the upsert/idempotency behavior is exercised
without network access or a shared database.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.database import (
    ConferenceRow,
    get_engine,
    merge_records,
    query_conferences,
    upsert_conferences,
)
from conference_agent.models import Conference, ConferenceSize, RemoteOption


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _conf(**overrides):
    base = dict(acronym="rsna", name="RSNA Annual Meeting", subcategory="radiology")
    base.update(overrides)
    return Conference(**base)


def test_upsert_then_query_round_trips(tmp_path):
    url = _db_url(tmp_path)
    conf = _conf(
        attendance=45000,
        attendance_year=2025,
        remote_option=RemoteOption.HYBRID,
        upcoming_start_date=date(2026, 11, 29),
        cost="$1,095 (member)",
    )
    written = upsert_conferences([conf], db_url=url)
    assert written == 1

    rows = query_conferences(db_url=url)
    assert len(rows) == 1
    got = rows[0]
    assert got.id == "RSNA"
    assert got.attendance == 45000
    assert got.attendance_year == 2025
    assert got.size == ConferenceSize.LARGE  # derived from attendance
    assert got.remote_option == RemoteOption.HYBRID
    assert got.upcoming_start_date == date(2026, 11, 29)
    assert got.cost == "$1,095 (member)"


def test_formats_round_trip_and_collapse_empty_to_none(tmp_path):
    url = _db_url(tmp_path)
    # A non-seed acronym so curated floors do not interfere; formats supplied out
    # of order are stored in the canonical abstract/paper/poster/oral order.
    upsert_conferences(
        [_conf(acronym="ZZT", name="ZZT", formats=["oral", "abstract", "poster"])],
        db_url=url,
    )
    got = query_conferences(db_url=url)[0]
    assert got.formats == ["abstract", "poster", "oral"]
    assert got.format == "abstract, poster, oral"

    # A conference with no formats stores NULL (collapsed empty), not "".
    upsert_conferences([_conf(acronym="NUL", name="No Formats")], db_url=url)
    none_row = {c.id: c for c in query_conferences(db_url=url)}["NUL"]
    assert none_row.formats == []


def test_merge_records_fills_formats_without_clobbering(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [_conf(acronym="ZZT", name="ZZT", formats=["abstract"])], db_url=url
    )
    # A partial record carrying only new formats updates them; the singular
    # "format" key and a delimited string are both accepted and normalized.
    merge_records([{"id": "ZZT", "format": "poster, oral, abstract"}], db_url=url)
    got = query_conferences(db_url=url)[0]
    assert got.formats == ["abstract", "poster", "oral"]
    # A record that omits formats leaves the stored value untouched.
    merge_records([{"id": "ZZT", "location": "Chicago, IL"}], db_url=url)
    got = query_conferences(db_url=url)[0]
    assert got.formats == ["abstract", "poster", "oral"]
    assert got.location == "Chicago, IL"


def test_upsert_applies_curated_link_floor_for_flagship(tmp_path):
    url = _db_url(tmp_path)
    # Discovery reports a weaker (homepage) URL for a flagship series...
    upsert_conferences([_conf(url="https://www.rsna.org")], db_url=url)
    # ...but the curated-link floor keeps the verified deep link.
    assert query_conferences(db_url=url)[0].url == "https://www.rsna.org/annual-meeting"


def test_upsert_keeps_discovered_url_when_not_curated(tmp_path):
    url = _db_url(tmp_path)
    # A non-curated series keeps whatever URL discovery found (no floor).
    found = "https://siim.org/page/annual_meeting"
    upsert_conferences(
        [_conf(acronym="SIIM", name="SIIM", url=found)], db_url=url
    )
    assert query_conferences(db_url=url)[0].url == found


def test_upsert_is_idempotent_on_id(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(cost="old")], db_url=url)
    upsert_conferences([_conf(cost="new")], db_url=url)  # same id (RSNA)

    rows = query_conferences(db_url=url)
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0].cost == "new"


def test_query_filters_by_subcategory_category_and_size(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(acronym="RSNA", attendance=45000),  # large, radiology -> medicine
            _conf(acronym="SIIM", name="SIIM", attendance=500),  # medium, radiology
            _conf(acronym="ASHG", name="ASHG", subcategory="genomics", attendance=12000),  # biology
        ],
        db_url=url,
    )

    assert {c.id for c in query_conferences(subcategory="radiology", db_url=url)} == {"RSNA", "SIIM"}
    assert {c.id for c in query_conferences(size="large", db_url=url)} == {"RSNA", "ASHG"}
    # The derived broad category groups the two radiology rows under medicine,
    # while genomics sorts into biology.
    assert {c.id for c in query_conferences(category="medicine", db_url=url)} == {"RSNA", "SIIM"}
    assert {c.id for c in query_conferences(category="biology", db_url=url)} == {"ASHG"}


def test_multi_subcategory_round_trips_and_filters_by_each_tag(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                acronym="MICCAI",
                name="Medical Image Computing",
                subcategories=["radiology", "machine learning"],
            )
        ],
        db_url=url,
    )
    got = query_conferences(db_url=url)[0]
    assert got.subcategories == ["radiology", "machine learning"]
    assert got.subcategory == "radiology, machine learning"
    # The derived category spans both domains (medicine + artificial intelligence).
    assert got.categories == ["medicine", "artificial intelligence"]
    assert got.category == "medicine, artificial intelligence"
    # A substring subcategory filter finds the row under either of its tags.
    assert {c.id for c in query_conferences(subcategory="radiology", db_url=url)} == {"MICCAI"}
    assert {c.id for c in query_conferences(subcategory="machine learning", db_url=url)} == {"MICCAI"}
    # And the broad category filter finds it under either derived bucket.
    assert {c.id for c in query_conferences(category="medicine", db_url=url)} == {"MICCAI"}
    assert {c.id for c in query_conferences(category="artificial intelligence", db_url=url)} == {"MICCAI"}


def test_seed_subcategory_floor_overrides_discovered_freetext(tmp_path):
    url = _db_url(tmp_path)
    # Discovery returns a descriptive blurb where a clean tag belongs...
    upsert_conferences(
        [_conf(acronym="ESICM", name="ESICM", subcategories=["intensive care / critical care medicine (europe-based)"])],
        db_url=url,
    )
    # ...but the curated seed tags win (ESICM's seed subcategory is critical care
    # medicine), and the derived category follows (-> medicine).
    got = query_conferences(db_url=url)[0]
    assert got.subcategories == ["critical care medicine"]
    assert got.categories == ["medicine"]

    # The floor also applies on the offline merge path.
    merge_records([{"id": "ESICM", "subcategory": "garbage, more garbage"}], db_url=url)
    assert query_conferences(db_url=url)[0].subcategories == ["critical care medicine"]


def test_non_seed_row_keeps_its_own_subcategories(tmp_path):
    url = _db_url(tmp_path)
    # An acronym that is not in the seed table is not subject to the floor.
    upsert_conferences(
        [_conf(acronym="NOVEL", name="Novel Workshop", subcategories=["robotics", "vision"])],
        db_url=url,
    )
    got = query_conferences(db_url=url)[0]
    assert got.subcategories == ["robotics", "vision"]
    # Neither tag is in the map, so the derived category is empty.
    assert got.categories == []


def test_recompute_categories_rederives_from_subcategories(tmp_path):
    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow, get_engine, recompute_categories

    url = _db_url(tmp_path)
    upsert_conferences([_conf(acronym="NOVEL", name="Novel", subcategory="radiology")], db_url=url)
    # Simulate a stale stored category (e.g. left over before the map changed).
    engine = get_engine(url)
    with Session(engine) as s:
        s.get(ConferenceRow, "NOVEL").category = "physics"
        s.commit()
    changed = recompute_categories(url)
    assert changed == 1
    assert query_conferences(db_url=url)[0].categories == ["medicine"]
    # Idempotent: a second pass changes nothing.
    assert recompute_categories(url) == 0


def test_month_columns_are_sql_computed_from_dates(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(upcoming_abstract_deadline=date(2026, 4, 8), upcoming_start_date=date(2026, 11, 29)),
            # No upcoming dates: months fall back to the prior edition's.
            _conf(acronym="ECR", name="ECR", prior_abstract_deadline=date(2025, 1, 15),
                  prior_start_date=date(2025, 3, 2)),
        ],
        db_url=url,
    )
    engine = get_engine(url)
    with Session(engine) as session:
        months = {
            r.id: (r.abstract_month, r.conference_month)
            for r in session.scalars(select(ConferenceRow))
        }
    assert months["RSNA"] == (4, 11)
    assert months["ECR"] == (1, 3)


def test_registration_text_round_trips(tmp_path):
    # Registration is free text (windows, not a date): both editions' values
    # persist as stored, and the model's ``registration`` property prefers the
    # upcoming edition, falling back to the prior one.
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                upcoming_registration="Early bird: Jan 5 - Mar 1; Regular: Mar 2 - conference",
                prior_registration="Registration opened June 2025",
            ),
            # Only the prior edition has registration info.
            _conf(acronym="ECR", name="ECR", prior_registration="Opens Sept 2025"),
            # Neither set: registration is blank.
            _conf(acronym="MICCAI", name="MICCAI"),
        ],
        db_url=url,
    )
    by_id = {c.id: c for c in query_conferences(db_url=url)}
    assert by_id["RSNA"].upcoming_registration.startswith("Early bird:")
    assert by_id["RSNA"].prior_registration == "Registration opened June 2025"
    # The displayed value prefers upcoming over prior.
    assert by_id["RSNA"].registration.startswith("Early bird:")
    assert by_id["ECR"].registration == "Opens Sept 2025"
    assert by_id["MICCAI"].registration is None


def test_abstract_and_paper_months_are_independent(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            # Each month is extracted from its own deadline, independently.
            _conf(
                upcoming_abstract_deadline=date(2026, 3, 10),
                upcoming_paper_deadline=date(2026, 5, 5),
            ),
            # Months fall back to the prior edition per-deadline; a missing
            # upcoming paper deadline drops to the prior one, not to the abstract.
            _conf(
                acronym="ICML", name="ICML",
                upcoming_abstract_deadline=date(2026, 1, 28),
                prior_paper_deadline=date(2025, 2, 1),
            ),
        ],
        db_url=url,
    )
    engine = get_engine(url)
    with Session(engine) as session:
        months = {
            r.id: (r.abstract_month, r.paper_month)
            for r in session.scalars(select(ConferenceRow))
        }
    assert months["RSNA"] == (3, 5)
    assert months["ICML"] == (1, 2)


def test_merge_records_fills_dates_without_clobbering(tmp_path):
    url = _db_url(tmp_path)
    # A seeded row carrying identity + url but no dates (as after seed_conferences).
    upsert_conferences(
        [_conf(attendance=45000, url="https://www.rsna.org")],
        db_url=url,
    )

    written = merge_records(
        [
            {
                "id": "RSNA",
                "upcoming_abstract_deadline": "2026-04-08",
                "upcoming_start_date": "2026-11-29",
                "upcoming_end_date": "2026-12-03",
                "location": "Chicago, IL",
                "remote_option": "hybrid",
                # url omitted on purpose -> must not be wiped
            }
        ],
        db_url=url,
    )
    assert written == 1

    got = query_conferences(db_url=url)[0]
    assert got.upcoming_abstract_deadline == date(2026, 4, 8)
    assert got.upcoming_start_date == date(2026, 11, 29)
    assert got.upcoming_end_date == date(2026, 12, 3)
    assert got.location == "Chicago, IL"
    assert got.remote_option == RemoteOption.HYBRID
    # Pre-existing fields the record did not mention are preserved. RSNA is a
    # flagship series, so upsert_conferences applies the curated-link floor: its
    # url is the verified deep link regardless of the homepage passed at seed.
    assert got.url == "https://www.rsna.org/annual-meeting"
    assert got.attendance == 45000
    assert got.size == ConferenceSize.LARGE
    assert got.name == "RSNA Annual Meeting"


def test_merge_records_recomputes_size_from_attendance(tmp_path):
    url = _db_url(tmp_path)
    # A row that starts with no attendance has no size.
    upsert_conferences([_conf(url="https://www.rsna.org")], db_url=url)
    assert query_conferences(db_url=url)[0].size is None

    # Merging an attendance figure (as a string, as from research) derives a size.
    merge_records([{"id": "RSNA", "attendance": "45000", "attendance_year": "2025"}], db_url=url)
    got = query_conferences(db_url=url)[0]
    assert got.attendance == 45000
    assert got.attendance_year == 2025
    assert got.size == ConferenceSize.LARGE


def test_recompute_sizes_rederives_stored_bucket(tmp_path):
    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow, get_engine, recompute_sizes

    url = _db_url(tmp_path)
    upsert_conferences([_conf(attendance=9000)], db_url=url)  # large under 1000-cutoff
    # Simulate a stale stored bucket (e.g. left over from an older threshold).
    engine = get_engine(url)
    with Session(engine) as s:
        s.get(ConferenceRow, "RSNA").size = "medium"
        s.commit()
    changed = recompute_sizes(url)
    assert changed == 1
    assert query_conferences(db_url=url)[0].size == ConferenceSize.LARGE
    # Idempotent: a second pass changes nothing.
    assert recompute_sizes(url) == 0


def test_month_fields_stored_and_recompute_rederives(tmp_path):
    import datetime

    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow, get_engine, recompute_months

    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                upcoming_start_date=datetime.date(2026, 11, 29),
                upcoming_abstract_deadline=datetime.date(2026, 5, 6),
            )
        ],
        db_url=url,
    )
    engine = get_engine(url)
    # Stored as real columns on write, derived from the dates.
    with Session(engine) as s:
        row = s.get(ConferenceRow, "RSNA")
        assert (row.conference_month, row.abstract_month, row.paper_month) == (11, 5, None)
        # Simulate stale stored months (e.g. an out-of-band date edit).
        row.conference_month = 1
        s.commit()
    changed = recompute_months(url)
    assert changed == 1
    with Session(engine) as s:
        assert s.get(ConferenceRow, "RSNA").conference_month == 11
    # Idempotent: a second pass changes nothing.
    assert recompute_months(url) == 0


def test_known_attendance_sources_maps_source_and_year(tmp_path):
    from conference_agent.database import known_attendance_sources

    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(acronym="RSNA", attendance=45000, attendance_year=2024,
                  attendance_source="https://rsna.org/2024/by-the-numbers"),
            _conf(acronym="SIIM", name="SIIM"),  # no source -> excluded
        ],
        db_url=url,
    )
    hints = known_attendance_sources(db_url=url)
    assert hints == {"RSNA": {"source": "https://rsna.org/2024/by-the-numbers", "year": 2024}}


def test_merge_records_skips_unknown_id_without_identity(tmp_path):
    url = _db_url(tmp_path)
    # No matching row and no name/category -> cannot insert, skipped.
    assert merge_records([{"id": "NEW", "upcoming_start_date": "2027-01-01"}], db_url=url) == 0
    assert query_conferences(db_url=url) == []

    # With identity supplied, an unknown id is inserted.
    assert (
        merge_records(
            [{"id": "NEW", "name": "New Meeting", "category": "radiology"}], db_url=url
        )
        == 1
    )
    assert {c.id for c in query_conferences(db_url=url)} == {"NEW"}
