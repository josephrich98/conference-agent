"""Offline tests for the discovery extraction/conversion helpers.

These cover the flat-string -> typed ``Conference`` mapping (the step that turns
the LLM's structured output into validated records). No network or LLM calls.
"""

from datetime import date

from conference_agent.config import (
    SEED_CONFERENCE_SOURCES,
    SEED_CONFERENCES,
    WEEKLY_CATEGORIES,
    monthly_categories,
    normalize_reputation,
    seed_categories,
    weekly_categories,
)
from conference_agent.discover import _ExtractedConference, _parse_date, _seed_checklist, _to_conference
from conference_agent.models import Conference, ConferenceTier, RemoteOption


def _extracted(**overrides):
    base = {f: "" for f in _ExtractedConference.model_fields}
    base.update(acronym="rsna", name="RSNA Annual Meeting", category="Radiology")
    base.update(overrides)
    return _ExtractedConference(**base)


def test_parse_date_handles_iso_blank_and_garbage():
    assert _parse_date("2026-11-29") == date(2026, 11, 29)
    assert _parse_date("") is None
    assert _parse_date("   ") is None
    assert _parse_date("not a date") is None


def test_to_conference_maps_dates_and_enums():
    conf = _to_conference(
        _extracted(
            upcoming_start_date="2026-11-29",
            upcoming_abstract_deadline="2026-04-08",
            location="Chicago, IL",
            reputation="Big",
            remote_option="Hybrid",
            cost="$1,095",
        )
    )
    assert conf is not None
    assert conf.id == "RSNA"
    assert conf.category == "radiology"  # normalized to lowercase
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.location == "Chicago, IL"
    assert conf.reputation == ConferenceTier.BIG
    assert conf.remote_option == RemoteOption.HYBRID
    assert conf.cost == "$1,095"


def test_reputation_policy_caps_non_flagship_at_medium():
    # A flagship conference keeps "big".
    ecr = _to_conference(_extracted(acronym="ecr", name="European Congress of Radiology", reputation="big"))
    assert ecr.reputation == ConferenceTier.BIG

    # Any other conference assigned "big" is demoted to "medium".
    spr = _to_conference(
        _extracted(acronym="spr", name="Society for Pediatric Radiology", reputation="big")
    )
    assert spr.reputation == ConferenceTier.MEDIUM

    # A non-flagship "small"/"medium" assignment passes through unchanged.
    small = _to_conference(_extracted(acronym="abc", name="Some Conf", reputation="small"))
    assert small.reputation == ConferenceTier.SMALL


def test_to_conference_blank_optionals_become_none():
    # Use a non-flagship acronym so the reputation policy doesn't force "big".
    conf = _to_conference(_extracted(acronym="abc", name="Some Conf"))
    assert conf.upcoming_start_date is None
    assert conf.reputation is None
    assert conf.remote_option is None
    assert conf.cost is None


def test_to_conference_invalid_enum_and_date_are_dropped():
    conf = _to_conference(
        _extracted(
            acronym="abc",
            name="Some Conf",
            reputation="enormous",
            remote_option="telepathic",
            upcoming_start_date="2026-13-40",
        )
    )
    assert conf.reputation is None
    assert conf.remote_option is None
    assert conf.upcoming_start_date is None


def test_to_conference_requires_identity_fields():
    assert _to_conference(_extracted(acronym="")) is None
    assert _to_conference(_extracted(name="")) is None


def test_seed_list_is_well_formed():
    # Each seed is a (acronym, name, category, tier) tuple with non-empty fields.
    assert SEED_CONFERENCES
    for acronym, name, category, tier in SEED_CONFERENCES:
        assert acronym.strip() and name.strip() and category.strip()
        assert isinstance(tier, ConferenceTier)
        # Seeds must satisfy the same identity/validation rules as discovered rows.
        conf = Conference(acronym=acronym, name=name, category=category)
        assert conf.id == acronym.upper()

    # Acronyms are the upsert key, so they must be unique (case-insensitive).
    acronyms = [a.upper() for a, _, _, _ in SEED_CONFERENCES]
    assert len(acronyms) == len(set(acronyms))


def test_seed_tiers_respect_reputation_policy():
    # Seed tiers should already match what normalize_reputation would produce,
    # so the seed list and the house policy never disagree.
    for acronym, _, _, tier in SEED_CONFERENCES:
        assert normalize_reputation(acronym, tier) == tier


def test_seed_checklist_includes_seeds_and_filters_by_category():
    # The checklist for a category lists exactly that category's seeds. Each line
    # is rendered as "- {acronym} — {name}", so match on that exact prefix rather
    # than a bare substring (e.g. so "ASH" does not match inside "ASHNR").
    checklist = _seed_checklist(["radiology"])
    for acronym, _, category, _ in SEED_CONFERENCES:
        line = f"- {acronym} — "
        if category == "radiology":
            assert line in checklist
        else:
            assert line not in checklist
    # A category with no seeds yields the explicit empty-state line.
    assert "no seeds" in _seed_checklist(["underwater basket weaving"]).lower()


def test_seed_sources_are_well_formed():
    assert SEED_CONFERENCE_SOURCES
    for label, url, note in SEED_CONFERENCE_SOURCES:
        assert label.strip() and note.strip()
        assert url.startswith("https://")


def test_cadence_partitions_the_seed_categories():
    weekly = weekly_categories()
    monthly = monthly_categories()
    # Weekly and monthly are disjoint and together cover every seeded category.
    assert set(weekly).isdisjoint(monthly)
    assert sorted(weekly + monthly) == seed_categories()
    # Weekly is exactly the seeded categories named in WEEKLY_CATEGORIES.
    assert set(weekly) == WEEKLY_CATEGORIES & set(seed_categories())


def test_weekly_categories_are_all_real_categories():
    # Every name in WEEKLY_CATEGORIES must correspond to an actual seed category,
    # otherwise the weekly job would silently refresh nothing for that name.
    assert WEEKLY_CATEGORIES <= set(seed_categories())
