"""Offline tests for the SQLAlchemy persistence layer.

Use a temporary SQLite file so the upsert/idempotency behavior is exercised
without network access or a shared database.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from conference_agent.database import (
    ConferenceRow,
    apply_refreshed_conferences,
    get_engine,
    merge_records,
    query_conferences,
    upsert_conferences,
)
from conference_agent.models import Conference, ConferenceSize, RemoteOption, name_id

# Series are indexed by name; RSNA's seed name, so the seed curation applies.
RSNA_NAME = "Radiological Society of North America Annual Meeting"
RSNA_ID = name_id(RSNA_NAME)
ESICM_NAME = "European Society of Intensive Care Medicine Annual Congress (LIVES)"


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _conf(**overrides):
    # Series are indexed by name, so a fixture that only overrides the acronym
    # gets a name of its own.
    acronym = overrides.get("acronym", "rsna")
    name = RSNA_NAME if acronym.upper() == "RSNA" else f"{acronym.upper()} Annual Meeting"
    base = dict(acronym="rsna", name=name, subcategory="radiology")
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
    assert got.id == RSNA_ID
    assert got.attendance == 45000
    assert got.attendance_year == 2025
    assert got.size == ConferenceSize.MASSIVE  # derived from attendance
    assert got.remote_option == RemoteOption.HYBRID
    assert got.upcoming_start_date == date(2026, 11, 29)
    assert got.cost == "$1,095 (member)"


def test_late_abstract_deadlines_round_trip_with_derived_month(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                acronym="CSHL-BIODATA",
                name="CSHL Biological Data Science",
                subcategory="genomics",
                upcoming_abstract_deadline=date(2026, 8, 28),
                upcoming_late_abstract_deadline=date(2026, 10, 1),
                prior_late_abstract_deadline=date(2024, 9, 26),
            )
        ],
        db_url=url,
    )
    got = query_conferences(db_url=url)[0]
    assert got.upcoming_abstract_deadline == date(2026, 8, 28)
    assert got.upcoming_late_abstract_deadline == date(2026, 10, 1)
    assert got.prior_late_abstract_deadline == date(2024, 9, 26)

    # The month column is derived on write from the upcoming date, like the
    # other month columns — never accepted as input.
    engine = get_engine(url)
    with Session(engine) as session:
        row = session.get(ConferenceRow, "cshl-biological-data-science")
        assert row.abstract_month == 8
        assert row.late_abstract_month == 10


def test_merge_records_fills_late_abstract_deadline(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [_conf(acronym="ASHG2", name="Human Genetics", subcategory="genomics")],
        db_url=url,
    )
    # A partial record carrying only the newly announced late-breaking window.
    merge_records(
        [{"id": "human-genetics", "upcoming_late_abstract_deadline": "2026-08-26"}], db_url=url
    )
    got = query_conferences(db_url=url)[0]
    assert got.upcoming_late_abstract_deadline == date(2026, 8, 26)
    assert got.name == "Human Genetics"  # untouched by the partial merge


def test_merge_records_re_derives_stale_month_columns(tmp_path):
    """A merged date must not leave the derived month column on the old value."""
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                acronym="ZZM",
                name="Month Derivation",
                subcategory="genomics",
                upcoming_abstract_deadline=date(2026, 10, 1),
            )
        ],
        db_url=url,
    )
    engine = get_engine(url)
    with Session(engine) as session:
        assert session.get(ConferenceRow, "month-derivation").abstract_month == 10

    # Correct the deadline to the earlier, primary one and add the late deadline.
    merge_records(
        [
            {
                "id": "month-derivation",
                "upcoming_abstract_deadline": "2026-08-28",
                "upcoming_late_abstract_deadline": "2026-10-01",
            }
        ],
        db_url=url,
    )
    with Session(engine) as session:
        row = session.get(ConferenceRow, "month-derivation")
        assert row.abstract_month == 8  # follows the new date, not the old one
        assert row.late_abstract_month == 10


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
    none_row = {c.id: c for c in query_conferences(db_url=url)}["no-formats"]
    assert none_row.formats == []


def test_merge_records_fills_formats_without_clobbering(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [_conf(acronym="ZZT", name="ZZT", formats=["abstract"])], db_url=url
    )
    # A partial record carrying only new formats updates them; the singular
    # "format" key and a delimited string are both accepted and normalized.
    merge_records([{"id": "zzt", "format": "poster, oral, abstract"}], db_url=url)
    got = query_conferences(db_url=url)[0]
    assert got.formats == ["abstract", "poster", "oral"]
    # A record that omits formats leaves the stored value untouched.
    merge_records([{"id": "zzt", "location": "Chicago, IL"}], db_url=url)
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
            _conf(acronym="RSNA", category="medicine", attendance=45000),  # massive
            _conf(acronym="SIIM", name="SIIM", category="medicine", attendance=500),  # medium
            _conf(acronym="ASHG", name="ASHG", subcategory="genomics", category="biology", attendance=12000),
        ],
        db_url=url,
    )

    assert {c.id for c in query_conferences(subcategory="radiology", db_url=url)} == {RSNA_ID, "siim"}
    assert {c.id for c in query_conferences(size="massive", db_url=url)} == {RSNA_ID, "ashg"}
    # The broad category filter groups rows by their stored categories.
    assert {c.id for c in query_conferences(category="medicine", db_url=url)} == {RSNA_ID, "siim"}
    assert {c.id for c in query_conferences(category="biology", db_url=url)} == {"ashg"}


def test_multi_subcategory_round_trips_and_filters_by_each_tag(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                acronym="MICCAI",
                name="Medical Image Computing",
                subcategories=["radiology", "machine learning"],
                categories=["medicine", "artificial intelligence"],
            )
        ],
        db_url=url,
    )
    got = query_conferences(db_url=url)[0]
    assert got.subcategories == ["radiology", "machine learning"]
    assert got.subcategory == "radiology, machine learning"
    # The stored category spans both domains (medicine + artificial intelligence).
    assert got.categories == ["medicine", "artificial intelligence"]
    assert got.category == "medicine, artificial intelligence"
    # A substring subcategory filter finds the row under either of its tags.
    assert {c.id for c in query_conferences(subcategory="radiology", db_url=url)} == {"medical-image-computing"}
    assert {c.id for c in query_conferences(subcategory="machine learning", db_url=url)} == {"medical-image-computing"}
    # And the broad category filter finds it under either bucket.
    assert {c.id for c in query_conferences(category="medicine", db_url=url)} == {"medical-image-computing"}
    assert {c.id for c in query_conferences(category="artificial intelligence", db_url=url)} == {"medical-image-computing"}


def test_seed_subcategory_floor_overrides_discovered_freetext(tmp_path):
    url = _db_url(tmp_path)
    # Discovery returns a descriptive blurb where a clean tag belongs...
    upsert_conferences(
        [_conf(acronym="ESICM", name=ESICM_NAME, subcategories=["intensive care / critical care medicine (europe-based)"])],
        db_url=url,
    )
    # ...but the curated seed tags win (ESICM's seed subcategory is critical care
    # medicine).
    got = query_conferences(db_url=url)[0]
    assert got.subcategories == ["critical care medicine"]

    # The floor also applies on the offline merge path.
    merge_records([{"id": name_id(ESICM_NAME), "subcategory": "garbage, more garbage"}], db_url=url)
    assert query_conferences(db_url=url)[0].subcategories == ["critical care medicine"]


def test_non_seed_row_keeps_its_own_subcategories(tmp_path):
    url = _db_url(tmp_path)
    # An acronym that is not in the seed table is not subject to the floor.
    upsert_conferences(
        [_conf(acronym="NOVEL", name="Novel Workshop", subcategories=["origami", "vision"])],
        db_url=url,
    )
    got = query_conferences(db_url=url)[0]
    assert got.subcategories == ["origami", "vision"]


def test_category_is_stored_and_kept_when_a_run_omits_it(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(acronym="NOVEL", name="Novel", subcategory="origami", category="art, medicine")], db_url=url)
    assert query_conferences(db_url=url)[0].categories == ["medicine", "art"]
    # A discovery run that reports no category keeps the stored one...
    upsert_conferences([_conf(acronym="NOVEL", name="Novel", subcategory="origami")], db_url=url)
    assert query_conferences(db_url=url)[0].categories == ["medicine", "art"]
    # ...one that reports a category replaces it, and a merge does the same.
    upsert_conferences([_conf(acronym="NOVEL", name="Novel", subcategory="origami", category="physics")], db_url=url)
    assert query_conferences(db_url=url)[0].categories == ["physics"]
    merge_records([{"id": "novel", "categories": ["stats"]}], db_url=url)
    assert query_conferences(db_url=url)[0].categories == ["stats"]
    merge_records([{"id": "novel", "notes": "x"}], db_url=url)
    assert query_conferences(db_url=url)[0].categories == ["stats"]


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
    assert months[RSNA_ID] == (4, 11)
    assert months["ecr"] == (1, 3)


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
    assert by_id[RSNA_ID].upcoming_registration.startswith("Early bird:")
    assert by_id[RSNA_ID].prior_registration == "Registration opened June 2025"
    # The displayed value prefers upcoming over prior.
    assert by_id[RSNA_ID].registration.startswith("Early bird:")
    assert by_id["ecr"].registration == "Opens Sept 2025"
    assert by_id["miccai"].registration is None


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
    assert months[RSNA_ID] == (3, 5)
    assert months["icml"] == (1, 2)


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
                "id": RSNA_ID,
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
    assert got.size == ConferenceSize.MASSIVE
    assert got.name == RSNA_NAME


def test_merge_records_recomputes_size_from_attendance(tmp_path):
    url = _db_url(tmp_path)
    # A row that starts with no attendance has no size.
    upsert_conferences([_conf(url="https://www.rsna.org")], db_url=url)
    assert query_conferences(db_url=url)[0].size is None

    # Merging an attendance figure (as a string, as from research) derives a size.
    merge_records([{"id": RSNA_ID, "attendance": "45000", "attendance_year": "2025"}], db_url=url)
    got = query_conferences(db_url=url)[0]
    assert got.attendance == 45000
    assert got.attendance_year == 2025
    assert got.size == ConferenceSize.MASSIVE


def test_recompute_sizes_rederives_stored_bucket(tmp_path):
    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow, get_engine, recompute_sizes

    url = _db_url(tmp_path)
    upsert_conferences([_conf(attendance=9000)], db_url=url)  # large under 1000-cutoff
    # Simulate a stale stored bucket (e.g. left over from an older threshold).
    engine = get_engine(url)
    with Session(engine) as s:
        s.get(ConferenceRow, RSNA_ID).size = "medium"
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
        row = s.get(ConferenceRow, RSNA_ID)
        assert (row.conference_month, row.abstract_month, row.paper_month) == (11, 5, None)
        # Simulate stale stored months (e.g. an out-of-band date edit).
        row.conference_month = 1
        s.commit()
    changed = recompute_months(url)
    assert changed == 1
    with Session(engine) as s:
        assert s.get(ConferenceRow, RSNA_ID).conference_month == 11
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
    assert hints == {f"RSNA — {RSNA_NAME}": {"source": "https://rsna.org/2024/by-the-numbers", "year": 2024}}


def test_merge_records_skips_unknown_id_without_identity(tmp_path):
    url = _db_url(tmp_path)
    # No matching row and no name/category -> cannot insert, skipped.
    assert merge_records([{"id": "NEW", "upcoming_start_date": "2027-01-01"}], db_url=url) == 0
    assert query_conferences(db_url=url) == []

    # With identity supplied, an unknown id is inserted.
    assert (
        merge_records(
            [{"id": "NEW", "name": "New Meeting", "subcategory": "radiology"}], db_url=url
        )
        == 1
    )
    assert {c.id for c in query_conferences(db_url=url)} == {"new-meeting"}


# --- Targeted refresh merge ---------------------------------------------------


def test_apply_refreshed_conferences_rolls_a_new_edition(tmp_path):
    url = f"sqlite:///{tmp_path / 'roll.db'}"
    upsert_conferences(
        [Conference(acronym="X", name="X Meeting", subcategory="radiology",
                    attendance=1200, cost="$500",
                    prior_start_date=date(2024, 11, 1),
                    upcoming_abstract_deadline=date(2025, 5, 1),
                    upcoming_late_abstract_deadline=date(2025, 6, 1),
                    upcoming_start_date=date(2025, 11, 2))],
        db_url=url,
    )
    # The refresh finds the 2026 edition and says nothing of attendance / cost
    # or of a late deadline for the new edition.
    written = apply_refreshed_conferences(
        [Conference(acronym="X", name="X Meeting", subcategory="radiology",
                    upcoming_abstract_deadline=date(2026, 5, 3),
                    upcoming_start_date=date(2026, 11, 1)),
         Conference(acronym="NEW", name="Not in table", subcategory="radiology")],
        db_url=url,
    )
    assert written == 1
    (row,) = query_conferences(db_url=url)
    # The 2025 edition moved to the prior slots, late deadline included ...
    assert row.prior_start_date == date(2025, 11, 2)
    assert row.prior_late_abstract_deadline == date(2025, 6, 1)
    # ... and did not linger in the upcoming ones.
    assert row.upcoming_late_abstract_deadline is None
    assert row.upcoming_abstract_deadline == date(2026, 5, 3)
    assert row.abstract_month == 5
    # Fields the refresh left blank are kept.
    assert (row.attendance, row.cost) == (1200, "$500")


def test_apply_refreshed_conferences_same_edition_extension(tmp_path):
    url = f"sqlite:///{tmp_path / 'ext.db'}"
    upsert_conferences(
        [Conference(acronym="X", name="X", subcategory="radiology",
                    prior_start_date=date(2025, 11, 1),
                    upcoming_abstract_deadline=date(2026, 5, 1),
                    upcoming_start_date=date(2026, 11, 2))],
        db_url=url,
    )
    apply_refreshed_conferences(
        [Conference(acronym="X", name="X", subcategory="radiology",
                    upcoming_abstract_deadline=date(2026, 5, 15),
                    upcoming_start_date=date(2026, 11, 2))],
        db_url=url,
    )
    (row,) = query_conferences(db_url=url)
    assert row.upcoming_abstract_deadline == date(2026, 5, 15)
    assert row.prior_start_date == date(2025, 11, 1)


# --- Name index ---------------------------------------------------------------


def test_legacy_acronym_ids_move_to_name_ids_with_aliases(tmp_path):
    from sqlalchemy import create_engine, text

    from conference_agent.database import former_ids, resolve_ids

    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE conferences (id TEXT PRIMARY KEY, acronym TEXT NOT NULL,"
            " name TEXT NOT NULL, subcategory TEXT NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO conferences VALUES ('RSNA','RSNA',:rsna,'radiology'),"
            " ('IDWEEK','IDWeek','IDWeek','infectious disease')"
        ), {"rsna": RSNA_NAME})
    engine.dispose()
    url = f"sqlite:///{path}"

    assert {c.id for c in query_conferences(db_url=url)} == {RSNA_ID, "idweek"}
    assert former_ids(url) == {"RSNA": RSNA_ID, "IDWEEK": "idweek"}
    # Records keyed by a former id (an older research file) still merge.
    assert merge_records([{"id": "RSNA", "location": "Chicago, IL"}], db_url=url) == 1
    assert resolve_ids(["RSNA", "nope"], url) == {"RSNA": RSNA_ID}


def test_legacy_rows_sharing_a_name_refuse_to_migrate(tmp_path):
    import pytest
    from sqlalchemy import create_engine, text

    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE conferences (id TEXT PRIMARY KEY, acronym TEXT NOT NULL,"
            " name TEXT NOT NULL, subcategory TEXT NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO conferences VALUES ('A','A','Same Name','radiology'),"
            " ('B','B','same name','radiology')"
        ))
    engine.dispose()
    with pytest.raises(RuntimeError, match="same name"):
        get_engine(f"sqlite:///{path}")


def test_discovery_with_a_reworded_name_updates_the_stored_series(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(acronym="RECOMB", name="Research in Computational Molecular Biology",
                              subcategory="bioinformatics")], db_url=url)
    # Same acronym, overlapping subcategory, different wording: the same series.
    upsert_conferences(
        [_conf(acronym="RECOMB",
               name="International Conference on Research in Computational Molecular Biology",
               subcategory="bioinformatics", upcoming_start_date=date(2027, 4, 1))],
        db_url=url,
    )
    [got] = query_conferences(db_url=url)
    assert got.name == "Research in Computational Molecular Biology"  # the index is kept
    assert got.upcoming_start_date == date(2027, 4, 1)


def test_same_acronym_in_another_field_is_a_separate_series(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(acronym="ICML", name="International Conference on Machine Learning",
                  subcategory="machine learning"),
            _conf(acronym="ICML", name="International Conference on Malignant Lymphoma",
                  subcategory="oncology"),
        ],
        db_url=url,
    )
    assert {c.id for c in query_conferences(db_url=url)} == {
        "international-conference-on-machine-learning",
        "international-conference-on-malignant-lymphoma",
    }
    # A merged record with only a name updates exactly the named series.
    merge_records(
        [{"name": "International Conference on Malignant Lymphoma", "location": "Lugano"}],
        db_url=url,
    )
    by_id = {c.id: c for c in query_conferences(db_url=url)}
    assert by_id["international-conference-on-malignant-lymphoma"].location == "Lugano"
    assert by_id["international-conference-on-machine-learning"].location is None


def test_refresh_merge_follows_the_matched_row(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(acronym="ZZR", name="Stored Name", subcategory="radiology")], db_url=url)
    apply_refreshed_conferences(
        [_conf(acronym="ZZR", name="Reworded Name", subcategory="radiology",
               upcoming_start_date=date(2027, 1, 5))],
        db_url=url,
    )
    [got] = query_conferences(db_url=url)
    assert (got.name, got.upcoming_start_date) == ("Stored Name", date(2027, 1, 5))


def test_delete_and_rename_helpers(tmp_path):
    import pytest

    from conference_agent.database import delete_conferences, rename_conference

    url = _db_url(tmp_path)
    upsert_conferences([_conf(acronym="AAA", name="Aaa"), _conf(acronym="BBB", name="Bbb")], db_url=url)
    with pytest.raises(ValueError, match="already names"):
        rename_conference("aaa", "BBB", db_url=url)
    assert rename_conference("aaa", "Aaa Renamed", db_url=url) == "aaa-renamed"
    assert delete_conferences(["bbb", "missing"], db_url=url) == 1
    assert [c.id for c in query_conferences(db_url=url)] == ["aaa-renamed"]


def _row(url, cid):
    with Session(get_engine(url)) as session:
        row = session.get(ConferenceRow, cid)
        session.expunge(row)
        return row


def test_roll_past_editions_moves_a_finished_edition_to_prior(tmp_path):
    from conference_agent.database import roll_past_editions

    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                prior_start_date=date(2025, 11, 30),
                upcoming_abstract_deadline=date(2026, 4, 8),
                upcoming_start_date=date(2026, 6, 1),
                upcoming_end_date=date(2026, 6, 5),
                upcoming_registration="Early bird: Jan 5 - Mar 1",
            )
        ],
        db_url=url,
    )
    # Still running on its last day: not rolled.
    assert roll_past_editions(url, today=date(2026, 6, 5))["rolled"] == []
    out = roll_past_editions(url, today=date(2026, 6, 6))
    assert out == {"duplicate": [], "rolled": [RSNA_ID]}
    row = _row(url, RSNA_ID)
    assert row.prior_start_date == date(2026, 6, 1)
    assert row.prior_end_date == date(2026, 6, 5)
    assert row.prior_abstract_deadline == date(2026, 4, 8)
    assert row.prior_registration == "Early bird: Jan 5 - Mar 1"
    assert row.upcoming_start_date is None and row.upcoming_abstract_deadline is None
    # The displayed month falls back to prior, so it is unchanged.
    assert row.conference_month == 6
    # Idempotent.
    assert roll_past_editions(url, today=date(2026, 6, 6)) == {"duplicate": [], "rolled": []}


def test_roll_past_editions_keeps_an_edition_with_a_deadline_ahead(tmp_path):
    from conference_agent.database import roll_past_editions

    url = _db_url(tmp_path)
    upsert_conferences(
        [_conf(upcoming_start_date=date(2026, 6, 1), upcoming_paper_deadline=date(2026, 8, 1))],
        db_url=url,
    )
    assert roll_past_editions(url, today=date(2026, 7, 1))["rolled"] == []


def test_roll_past_editions_merges_a_duplicated_edition(tmp_path):
    from conference_agent.database import roll_past_editions

    url = _db_url(tmp_path)
    upsert_conferences(
        [_conf(upcoming_start_date=date(2026, 12, 1), upcoming_end_date=date(2026, 12, 4))],
        db_url=url,
    )
    # Write the duplicate straight to the row (the model would collapse it).
    with Session(get_engine(url)) as session:
        row = session.get(ConferenceRow, RSNA_ID)
        row.prior_start_date = date(2026, 12, 1)
        row.prior_abstract_deadline = date(2026, 5, 1)
        session.commit()
    out = roll_past_editions(url, today=date(2026, 10, 1))
    assert out == {"duplicate": [RSNA_ID], "rolled": []}
    row = _row(url, RSNA_ID)
    assert row.prior_start_date is None and row.prior_abstract_deadline is None
    assert row.upcoming_start_date == date(2026, 12, 1)
    assert row.upcoming_abstract_deadline == date(2026, 5, 1)  # filled from prior


def test_location_and_cost_roll_with_their_edition(tmp_path):
    from conference_agent.database import roll_past_editions

    url = _db_url(tmp_path)
    upsert_conferences(
        [_conf(upcoming_start_date=date(2026, 6, 1), location="Boston, MA", cost="$500")],
        db_url=url,
    )
    row = _row(url, RSNA_ID)
    # The legacy single-value keys fill the upcoming slots.
    assert (row.upcoming_location, row.upcoming_cost) == ("Boston, MA", "$500")
    roll_past_editions(url, today=date(2026, 7, 1))
    row = _row(url, RSNA_ID)
    assert (row.prior_location, row.prior_cost) == ("Boston, MA", "$500")
    assert row.upcoming_location is None and row.upcoming_cost is None
    conf = query_conferences(db_url=url)[0]
    assert (conf.location, conf.cost) == ("Boston, MA", "$500")


def test_stable_location_survives_a_roll_and_a_refresh(tmp_path):
    from conference_agent.database import roll_past_editions

    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf(
                prior_start_date=date(2025, 6, 1),
                prior_location="Chicago, IL",
                upcoming_start_date=date(2026, 6, 1),
                stable_location=True,
            )
        ],
        db_url=url,
    )
    roll_past_editions(url, today=date(2026, 7, 1))
    row = _row(url, RSNA_ID)
    # The finished edition had no location of its own; the stable one is kept.
    assert row.prior_location == "Chicago, IL" and row.stable_location
    # A discovery run that does not report the flag leaves it set.
    upsert_conferences([_conf(prior_start_date=date(2026, 6, 1), prior_location="Chicago, IL")], db_url=url)
    assert _row(url, RSNA_ID).stable_location
    # An explicit false from a record clears it.
    merge_records([{"id": RSNA_ID, "stable_location": "false"}], db_url=url)
    assert not _row(url, RSNA_ID).stable_location


def test_merge_records_accepts_legacy_location_and_cost_keys(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf()], db_url=url)
    merge_records([{"id": RSNA_ID, "location": "Chicago, IL", "prior_cost": "$100"}], db_url=url)
    row = _row(url, RSNA_ID)
    assert (row.upcoming_location, row.prior_cost, row.upcoming_cost) == ("Chicago, IL", "$100", None)


def test_legacy_location_and_cost_columns_move_to_the_shown_edition(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE conferences (id VARCHAR PRIMARY KEY, acronym VARCHAR NOT NULL, "
        "name VARCHAR NOT NULL, subcategory VARCHAR NOT NULL, upcoming_start_date DATE, "
        "prior_start_date DATE, location TEXT, cost TEXT)"
    )
    con.executemany(
        "INSERT INTO conferences VALUES (?, ?, ?, 'radiology', ?, ?, ?, ?)",
        [
            ("a-meeting", "A", "A Meeting", "2026-06-01", None, "Boston", "$1"),
            ("b-meeting", "B", "B Meeting", None, "2025-06-01", "Vienna", None),
        ],
    )
    con.commit()
    con.close()
    url = f"sqlite:///{path}"
    get_engine(url)
    a, b = _row(url, "a-meeting"), _row(url, "b-meeting")
    assert (a.upcoming_location, a.upcoming_cost, a.prior_location) == ("Boston", "$1", None)
    assert (b.prior_location, b.upcoming_location) == ("Vienna", None)
    con = sqlite3.connect(path)
    columns = {r[1] for r in con.execute("PRAGMA table_info(conferences)")}
    con.close()
    assert "location" not in columns and "cost" not in columns


# --- Deadline extensions ----------------------------------------------------


def _rsna_row(db_url):
    with Session(get_engine(db_url)) as session:
        row = session.get(ConferenceRow, RSNA_ID)
        session.expunge(row)
        return row


def test_a_later_deadline_for_the_same_edition_is_recorded_as_an_extension(tmp_path):
    from datetime import timedelta

    db_url = _db_url(tmp_path)
    due = date.today() + timedelta(days=5)
    start = due + timedelta(days=60)
    upsert_conferences([_conf(upcoming_abstract_deadline=due, upcoming_start_date=start)], db_url)
    assert _rsna_row(db_url).abstract_deadline_extension is None

    first = due + timedelta(days=5)
    upsert_conferences([_conf(upcoming_abstract_deadline=first, upcoming_start_date=start)], db_url)
    second = first + timedelta(days=7)
    merge_records([{"id": RSNA_ID, "upcoming_abstract_deadline": second.isoformat()}], db_url)
    # An unchanged deadline adds nothing.
    merge_records([{"id": RSNA_ID, "upcoming_abstract_deadline": second.isoformat()}], db_url)

    fmt = "%m/%d/%Y"
    row = _rsna_row(db_url)
    assert row.abstract_deadline_extension == (
        f"{due:{fmt}} – {first:{fmt}}\n{first:{fmt}} – {second:{fmt}}"
    )
    assert row.paper_deadline_extension is None
    assert row.late_abstract_deadline_extension is None


def test_new_editions_corrections_and_earlier_dates_are_not_extensions(tmp_path):
    from datetime import timedelta

    from conference_agent.database import _record_extensions

    today = date(2026, 10, 1)
    row = ConferenceRow(id="x", acronym="X", name="X", subcategory="radiology")
    row.upcoming_start_date = date(2026, 12, 1)
    cases = [
        # (old deadline, new deadline, new start date)
        (date(2026, 10, 5), date(2027, 10, 5), date(2027, 12, 1)),  # next edition
        (date(2026, 10, 5), date(2026, 10, 9), date(2027, 12, 1)),  # start moved a year
        (date(2027, 3, 1), date(2027, 3, 8), date(2026, 12, 1)),  # months ahead: correction
        (date(2026, 10, 5), date(2026, 10, 1), date(2026, 12, 1)),  # moved earlier
        (date(2026, 10, 5), today + timedelta(days=200), date(2026, 12, 1)),  # too large a jump
    ]
    for old, new, start in cases:
        before = {"abstract": old, "late_abstract": None, "paper": None, "start": row.upcoming_start_date}
        row.upcoming_abstract_deadline, row.upcoming_start_date = new, start
        assert not _record_extensions(row, before, today=today)
        row.upcoming_start_date = date(2026, 12, 1)
    assert row.abstract_deadline_extension is None


def test_extensions_older_than_five_years_are_dropped(tmp_path):
    from conference_agent.database import _prune_extensions

    value = "09/30/2020 – 10/05/2020\n09/30/2021 – 10/05/2021\n09/30/2021 – 10/05/2021"
    assert _prune_extensions(value, date(2026, 10, 1)) == "09/30/2021 – 10/05/2021"
    assert _prune_extensions("09/30/2020 – 10/05/2020", date(2026, 10, 1)) is None


def test_extensions_reach_the_page_but_not_the_csv_columns(tmp_path):
    from web.app import _RESULT_COLUMNS, _row_to_dict

    db_url = _db_url(tmp_path)
    upsert_conferences([_conf()], db_url)
    # Serialized for the table's hover marker, but never a table / CSV column.
    assert "abstract_deadline_extension" in _row_to_dict(_rsna_row(db_url))
    assert not any("extension" in col for col in _RESULT_COLUMNS)
