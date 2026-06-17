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
from conference_agent.models import Conference, ConferenceTier, RemoteOption


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _conf(**overrides):
    base = dict(acronym="rsna", name="RSNA Annual Meeting", category="radiology")
    base.update(overrides)
    return Conference(**base)


def test_upsert_then_query_round_trips(tmp_path):
    url = _db_url(tmp_path)
    conf = _conf(
        reputation=ConferenceTier.BIG,
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
    assert got.reputation == ConferenceTier.BIG
    assert got.remote_option == RemoteOption.HYBRID
    assert got.upcoming_start_date == date(2026, 11, 29)
    assert got.cost == "$1,095 (member)"


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


def test_query_filters_by_category_and_reputation(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(acronym="RSNA", reputation=ConferenceTier.BIG),
            _conf(acronym="SIIM", name="SIIM", reputation=ConferenceTier.MEDIUM),
            _conf(acronym="ASHG", name="ASHG", category="genomics", reputation=ConferenceTier.BIG),
        ],
        db_url=url,
    )

    assert {c.id for c in query_conferences(category="radiology", db_url=url)} == {"RSNA", "SIIM"}
    assert {c.id for c in query_conferences(reputation="big", db_url=url)} == {"RSNA", "ASHG"}


def test_multi_category_round_trips_and_filters_by_each_tag(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                acronym="SPR",
                name="Society for Pediatric Radiology",
                categories=["radiology", "pediatrics"],
            )
        ],
        db_url=url,
    )
    got = query_conferences(db_url=url)[0]
    assert got.categories == ["radiology", "pediatrics"]
    assert got.category == "radiology, pediatrics"
    # A substring category filter finds the row under either of its tags.
    assert {c.id for c in query_conferences(category="radiology", db_url=url)} == {"SPR"}
    assert {c.id for c in query_conferences(category="pediatrics", db_url=url)} == {"SPR"}


def test_seed_category_floor_overrides_discovered_freetext(tmp_path):
    url = _db_url(tmp_path)
    # Discovery returns a descriptive blurb where a clean tag belongs...
    upsert_conferences(
        [_conf(acronym="ESICM", name="ESICM", categories=["intensive care / critical care medicine (europe-based)"])],
        db_url=url,
    )
    # ...but the curated seed tags win (ESICM's seed category is critical care medicine).
    assert query_conferences(db_url=url)[0].categories == ["critical care medicine"]

    # The floor also applies on the offline merge path.
    merge_records([{"id": "ESICM", "category": "garbage, more garbage"}], db_url=url)
    assert query_conferences(db_url=url)[0].categories == ["critical care medicine"]


def test_non_seed_row_keeps_its_own_categories(tmp_path):
    url = _db_url(tmp_path)
    # An acronym that is not in the seed table is not subject to the floor.
    upsert_conferences(
        [_conf(acronym="NOVEL", name="Novel Workshop", categories=["robotics", "vision"])],
        db_url=url,
    )
    assert query_conferences(db_url=url)[0].categories == ["robotics", "vision"]


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
        [_conf(reputation=ConferenceTier.BIG, url="https://www.rsna.org")],
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
    assert got.reputation == ConferenceTier.BIG
    assert got.name == "RSNA Annual Meeting"


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
