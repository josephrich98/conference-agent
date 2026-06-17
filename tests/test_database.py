"""Offline tests for the SQLAlchemy persistence layer.

Use a temporary SQLite file so the upsert/idempotency behavior is exercised
without network access or a shared database.
"""

from datetime import date

from conference_agent.database import (
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
