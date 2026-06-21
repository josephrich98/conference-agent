"""Offline unit tests for the Conference schema.

These exercise only the typed model so CI stays hermetic (no network, no LLM).
"""

from datetime import date

from conference_agent.models import (
    CATEGORIES,
    SUBCATEGORY_TO_CATEGORY,
    Conference,
    ConferenceSize,
    RemoteOption,
    categories_for_subcategories,
    normalize_formats,
)


def _conf(**overrides):
    base = dict(acronym="rsna", name="Radiological Society of North America", subcategory="radiology")
    base.update(overrides)
    return Conference(**base)


def test_conference_id_is_upper_acronym():
    assert _conf().id == "RSNA"


def test_size_is_derived_from_attendance():
    # Size is a computed bucket of the attendance figure, not a stored label.
    # Buckets track the thresholds in models.py (large >= 1000, medium >= 100).
    assert _conf(attendance=45000).size == ConferenceSize.LARGE
    assert _conf(attendance=1000).size == ConferenceSize.LARGE  # inclusive lower bound
    assert _conf(attendance=500).size == ConferenceSize.MEDIUM
    assert _conf(attendance=100).size == ConferenceSize.MEDIUM  # inclusive lower bound
    assert _conf(attendance=50).size == ConferenceSize.SMALL
    # Unknown attendance leaves size blank rather than guessing.
    assert _conf().size is None


def test_attendance_display_includes_year():
    assert _conf(attendance=45000, attendance_year=2025).attendance_display == "45,000 (2025)"
    # The year is optional; the bare figure is still formatted with separators.
    assert _conf(attendance=45000).attendance_display == "45,000"
    assert _conf().attendance_display is None


def test_remote_option_is_controlled_enum():
    conf = _conf(remote_option=RemoteOption.HYBRID)
    assert conf.remote_option.value == "hybrid"


def test_optional_fields_default_to_none():
    conf = _conf(acronym="ARRS", name="American Roentgen Ray Society")
    assert conf.prior_abstract_deadline is None
    assert conf.upcoming_start_date is None
    assert conf.cost is None
    assert conf.attendance is None
    assert conf.size is None


def test_prior_and_upcoming_dates_round_trip():
    conf = _conf(
        prior_abstract_deadline=date(2025, 4, 8),
        prior_start_date=date(2025, 11, 30),
        prior_end_date=date(2025, 12, 4),
        upcoming_abstract_deadline=date(2026, 4, 8),
        upcoming_start_date=date(2026, 11, 29),
        upcoming_end_date=date(2026, 12, 3),
    )
    assert conf.prior_start_date < conf.upcoming_start_date
    assert conf.upcoming_abstract_deadline < conf.upcoming_start_date


def test_upcoming_year_derived_from_start_date():
    assert _conf(upcoming_start_date=date(2026, 11, 29)).upcoming_year == 2026
    assert _conf().upcoming_year is None


def test_single_subcategory_string_becomes_a_list():
    conf = _conf()  # built with subcategory="radiology"
    assert conf.subcategories == ["radiology"]
    assert conf.subcategory == "radiology"
    # The broad category is derived from the subcategory (radiology -> medicine).
    assert conf.categories == ["medicine"]
    assert conf.category == "medicine"


def test_multiple_subcategories_accepted_as_list_or_string():
    # As an explicit list (canonical field name).
    a = _conf(subcategories=["Radiology", "Pediatrics"])
    assert a.subcategories == ["radiology", "pediatrics"]
    assert a.subcategory == "radiology, pediatrics"
    # Both subcategories map to medicine, so the derived category de-dupes to one.
    assert a.categories == ["medicine"]
    # As a delimited string via the singular alias; normalized + de-duped.
    b = _conf(subcategory="radiology, machine learning, radiology")
    assert b.subcategories == ["radiology", "machine learning"]


def test_category_is_derived_from_subcategories_across_domains():
    # A multi-domain series carries every category its subcategories imply, in
    # canonical CATEGORIES order (medicine before artificial intelligence).
    conf = _conf(subcategories=["radiology", "machine learning"])
    assert conf.categories == ["medicine", "artificial intelligence"]
    assert conf.category == "medicine, artificial intelligence"
    # genomics -> biology.
    assert _conf(subcategory="genomics").categories == ["biology"]


def test_category_is_blank_when_subcategory_is_unmapped():
    # An unfamiliar subcategory (not in the map) contributes no category rather
    # than raising, so a novel tag degrades gracefully.
    conf = _conf(subcategories=["underwater basket weaving"])
    assert conf.subcategories == ["underwater basket weaving"]
    assert conf.categories == []
    assert conf.category == ""


def test_categories_for_subcategories_helper():
    assert categories_for_subcategories(["radiology"]) == ["medicine"]
    assert categories_for_subcategories(["machine learning", "genomics"]) == [
        "biology",
        "artificial intelligence",
    ]
    assert categories_for_subcategories([]) == []
    assert categories_for_subcategories(None) == []


def test_subcategory_map_values_are_valid_categories():
    # Every mapped category must be one of the ten controlled buckets, so the
    # derived column never produces an out-of-vocabulary value.
    assert set(SUBCATEGORY_TO_CATEGORY.values()) <= set(CATEGORIES)


def test_formats_default_to_empty():
    conf = _conf()
    assert conf.formats == []
    assert conf.format == ""


def test_formats_accepted_as_list_or_string_and_canonically_ordered():
    # As an explicit list (canonical field name); kept in the canonical
    # abstract/paper/poster/oral order regardless of the order supplied.
    a = _conf(formats=["Oral", "abstract", "poster"])
    assert a.formats == ["abstract", "poster", "oral"]
    assert a.format == "abstract, poster, oral"
    # As a delimited string via the singular ``format`` alias; de-duplicated.
    b = _conf(format="poster, oral, poster")
    assert b.formats == ["poster", "oral"]
    assert b.format == "poster, oral"


def test_formats_drop_unrecognized_tokens():
    # Tokens outside the controlled vocabulary are dropped so the column stays clean.
    assert _conf(format="abstract, keynote, workshop").formats == ["abstract"]
    assert normalize_formats("paper; poster; demo") == ["paper", "poster"]
    assert normalize_formats(None) == []


def test_conference_month_derived_from_dates():
    # Upcoming wins; falls back to prior; None when neither is set.
    assert _conf(upcoming_start_date=date(2026, 11, 29)).conference_month == 11
    assert _conf(prior_start_date=date(2025, 5, 4)).conference_month == 5
    assert _conf(
        prior_start_date=date(2025, 5, 4), upcoming_start_date=date(2026, 11, 29)
    ).conference_month == 11
    assert _conf().conference_month is None
    assert _conf(upcoming_start_date=date(2026, 11, 29)).conference_month_name == "November"


def test_abstract_month_derived_from_abstract_deadline():
    # Upcoming wins; falls back to prior; None when neither is set.
    assert _conf(upcoming_abstract_deadline=date(2026, 4, 8)).abstract_month == 4
    assert _conf(prior_abstract_deadline=date(2025, 3, 1)).abstract_month == 3
    assert _conf(
        prior_abstract_deadline=date(2025, 3, 1), upcoming_abstract_deadline=date(2026, 4, 8)
    ).abstract_month == 4
    assert _conf().abstract_month is None
    assert _conf(upcoming_abstract_deadline=date(2026, 4, 8)).abstract_month_name == "April"


def test_paper_month_derived_from_paper_deadline():
    # Each month is derived independently from its own deadline.
    assert _conf(upcoming_paper_deadline=date(2026, 5, 13)).paper_month == 5
    assert _conf(prior_paper_deadline=date(2025, 6, 4)).paper_month == 6
    assert _conf().paper_month is None
    # The abstract and paper months are unrelated — neither leaks into the other.
    conf = _conf(
        upcoming_abstract_deadline=date(2026, 3, 10),
        upcoming_paper_deadline=date(2026, 6, 4),
    )
    assert (conf.abstract_month, conf.paper_month) == (3, 6)
    assert _conf(upcoming_paper_deadline=date(2026, 5, 13)).paper_month_name == "May"


def test_registration_is_free_text_preferring_upcoming():
    # Registration is free text (windows, not a date). The ``registration``
    # property prefers the upcoming edition, falls back to prior, None when unset.
    assert _conf(upcoming_registration="Early bird: Jan 5 - Mar 1").registration == (
        "Early bird: Jan 5 - Mar 1"
    )
    assert _conf(prior_registration="Opened June 2025").registration == "Opened June 2025"
    assert _conf(
        prior_registration="Opened June 2025",
        upcoming_registration="Early bird: Jan 5 - Mar 1",
    ).registration == "Early bird: Jan 5 - Mar 1"
    assert _conf().registration is None
    assert _conf().upcoming_registration is None


def test_registration_date_aliases_accepted_for_back_compat():
    # The old date-style keys still load (as text) so older records/exports ingest.
    c = _conf(
        upcoming_registration_date="2026-03-02",
        prior_registration_date="2025-08-01",
    )
    assert c.upcoming_registration == "2026-03-02"
    assert c.prior_registration == "2025-08-01"
