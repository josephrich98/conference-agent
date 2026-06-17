"""Offline unit tests for the Conference schema.

These exercise only the typed model so CI stays hermetic (no network, no LLM).
"""

from datetime import date

from conference_agent.models import Conference, ConferenceTier, RemoteOption


def _conf(**overrides):
    base = dict(acronym="rsna", name="Radiological Society of North America", category="radiology")
    base.update(overrides)
    return Conference(**base)


def test_conference_id_is_upper_acronym():
    assert _conf().id == "RSNA"


def test_reputation_is_controlled_enum():
    conf = _conf(reputation=ConferenceTier.BIG)
    assert conf.reputation == ConferenceTier.BIG
    assert conf.reputation.value == "big"


def test_remote_option_is_controlled_enum():
    conf = _conf(remote_option=RemoteOption.HYBRID)
    assert conf.remote_option.value == "hybrid"


def test_optional_fields_default_to_none():
    conf = _conf(acronym="ARRS", name="American Roentgen Ray Society")
    assert conf.prior_abstract_deadline is None
    assert conf.upcoming_start_date is None
    assert conf.cost is None
    assert conf.reputation is None


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


def test_single_category_string_becomes_a_list():
    conf = _conf()  # built with category="radiology"
    assert conf.categories == ["radiology"]
    assert conf.category == "radiology"


def test_multiple_categories_accepted_as_list_or_string():
    # As an explicit list (canonical field name).
    a = _conf(categories=["Radiology", "Pediatrics"])
    assert a.categories == ["radiology", "pediatrics"]
    assert a.category == "radiology, pediatrics"
    # As a delimited string via the legacy singular alias; normalized + de-duped.
    b = _conf(category="radiology, machine learning, radiology")
    assert b.categories == ["radiology", "machine learning"]


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
