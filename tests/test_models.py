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
