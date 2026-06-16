"""Offline unit tests for the Conference schema.

These exercise only the typed model so CI stays hermetic (no network, no LLM).
"""

from datetime import date

from conference_agent.models import Conference, ConferenceTier


def test_conference_id_is_acronym_and_year():
    conf = Conference(acronym="rsna", name="Radiological Society of North America", year=2026)
    assert conf.id == "RSNA-2026"


def test_tier_is_controlled_enum():
    conf = Conference(
        acronym="SPR",
        name="Society for Pediatric Radiology",
        year=2026,
        tier=ConferenceTier.MEDIUM,
    )
    assert conf.tier == ConferenceTier.MEDIUM
    assert conf.tier.value == "medium"


def test_optional_fields_default_to_none():
    conf = Conference(acronym="ARRS", name="American Roentgen Ray Society", year=2026)
    assert conf.abstract_deadline is None
    assert conf.start_date is None
    assert conf.location is None


def test_dates_round_trip():
    conf = Conference(
        acronym="RSNA",
        name="Radiological Society of North America",
        year=2026,
        abstract_deadline=date(2026, 4, 8),
        start_date=date(2026, 11, 29),
        end_date=date(2026, 12, 3),
    )
    assert conf.start_date < conf.end_date
    assert conf.abstract_deadline < conf.start_date
