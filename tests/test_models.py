"""Offline unit tests for the Conference schema.

These exercise only the typed model so CI stays hermetic (no network, no LLM).
"""

from datetime import date

from conference_agent.models import (
    CATEGORIES,
    Conference,
    ConferenceSize,
    RemoteOption,
    normalize_categories,
    normalize_formats,
)


def _conf(**overrides):
    base = dict(acronym="rsna", name="Radiological Society of North America", subcategory="radiology")
    base.update(overrides)
    return Conference(**base)


def test_conference_id_is_the_name_slug():
    # Series are indexed by name: two series may share an acronym.
    assert _conf().id == "radiological-society-of-north-america"
    assert _conf(name="  Café   Meeting, 2027! ").id == "cafe-meeting-2027"
    assert _conf(acronym="ISMB", name="A").id != _conf(acronym="ISMB", name="B").id


def test_name_without_letters_or_digits_is_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _conf(name="—")


def test_size_is_derived_from_attendance():
    # Size is a computed bucket of the attendance figure, not a stored label.
    # Buckets track the thresholds in models.py (massive >= 10000, large >= 1000,
    # medium >= 250).
    assert _conf(attendance=45000).size == ConferenceSize.MASSIVE
    assert _conf(attendance=10000).size == ConferenceSize.MASSIVE  # inclusive lower bound
    assert _conf(attendance=9999).size == ConferenceSize.LARGE
    assert _conf(attendance=1000).size == ConferenceSize.LARGE  # inclusive lower bound
    assert _conf(attendance=500).size == ConferenceSize.MEDIUM
    assert _conf(attendance=250).size == ConferenceSize.MEDIUM  # inclusive lower bound
    assert _conf(attendance=249).size == ConferenceSize.SMALL
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
    # Category is an input of its own, so none is implied by the subcategory.
    assert conf.categories == []
    assert conf.category == ""


def test_multiple_subcategories_accepted_as_list_or_string():
    # As an explicit list (canonical field name).
    a = _conf(subcategories=["Radiology", "Pediatrics"])
    assert a.subcategories == ["radiology", "pediatrics"]
    assert a.subcategory == "radiology, pediatrics"
    # As a delimited string via the singular alias; normalized + de-duped.
    b = _conf(subcategory="radiology, machine learning, radiology")
    assert b.subcategories == ["radiology", "machine learning"]


def test_categories_are_input_and_normalized():
    # Accepted as a list or a delimited string, under either key; the suggested
    # categories come first in canonical order, then custom ones as given.
    conf = _conf(category="Artificial Intelligence; robotics ethics, medicine, medicine")
    assert conf.categories == ["medicine", "artificial intelligence", "robotics ethics"]
    assert conf.category == "medicine, artificial intelligence, robotics ethics"
    assert _conf(categories=["biology"]).categories == ["biology"]
    # Independent of the subcategories.
    assert _conf(subcategory="genomics", category="physics").categories == ["physics"]


def test_normalize_categories_helper():
    assert normalize_categories("stats, medicine") == ["medicine", "stats"]
    assert normalize_categories(["Custom", "medicine"]) == ["medicine", "custom"]
    assert normalize_categories(None) == []
    assert set(CATEGORIES) >= {"medicine", "artificial intelligence"}


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


def test_late_abstract_month_derived_from_late_deadline():
    # The late abstract deadline is the second, later one an edition publishes
    # (a poster-only deadline, or a late-breaking round). Its month is derived
    # exactly like abstract_month: upcoming wins, falls back to prior.
    assert _conf(upcoming_late_abstract_deadline=date(2026, 10, 1)).late_abstract_month == 10
    assert _conf(prior_late_abstract_deadline=date(2025, 9, 12)).late_abstract_month == 9
    assert _conf(
        prior_late_abstract_deadline=date(2025, 9, 12),
        upcoming_late_abstract_deadline=date(2026, 10, 1),
    ).late_abstract_month == 10
    assert _conf().late_abstract_month is None
    assert (
        _conf(upcoming_late_abstract_deadline=date(2026, 10, 1)).late_abstract_month_name
        == "October"
    )


def test_late_abstract_deadline_is_independent_of_the_main_one():
    # CSHL Biological Data Science: talks Aug 28, posters Oct 1. The main
    # abstract field holds the earlier, primary deadline; neither month leaks.
    conf = _conf(
        upcoming_abstract_deadline=date(2026, 8, 28),
        upcoming_late_abstract_deadline=date(2026, 10, 1),
    )
    assert (conf.abstract_month, conf.late_abstract_month) == (8, 10)
    # A series with only one abstract deadline leaves the late one blank.
    single = _conf(upcoming_abstract_deadline=date(2026, 8, 28))
    assert single.upcoming_late_abstract_deadline is None
    assert single.late_abstract_month is None


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


def test_duplicated_prior_edition_collapses_into_upcoming():
    conf = Conference(
        acronym="ICML",
        name="International Conference on Machine Learning",
        subcategory="machine learning",
        prior_start_date="2026-07-06",
        prior_abstract_deadline="2026-01-23",
        prior_registration_date="Opens March",
        upcoming_start_date=date(2026, 7, 6),
        upcoming_end_date=date(2026, 7, 11),
    )
    assert conf.prior_start_date is None and conf.prior_abstract_deadline is None
    assert conf.prior_registration is None
    assert conf.upcoming_start_date == date(2026, 7, 6)
    assert conf.upcoming_end_date == date(2026, 7, 11)
    assert conf.upcoming_abstract_deadline == date(2026, 1, 23)
    assert conf.upcoming_registration == "Opens March"


def test_distinct_editions_are_left_alone():
    conf = Conference(
        acronym="ICML",
        name="International Conference on Machine Learning",
        subcategory="machine learning",
        prior_start_date=date(2025, 7, 13),
        upcoming_start_date=date(2026, 7, 6),
    )
    assert conf.prior_start_date == date(2025, 7, 13)
