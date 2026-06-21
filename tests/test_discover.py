"""Offline tests for the discovery extraction/conversion helpers.

These cover the flat-string -> typed ``Conference`` mapping (the step that turns
the LLM's structured output into validated records). No network or LLM calls.
"""

import json
from datetime import date

import pytest

import conference_agent.discover as discover
from conference_agent.config import (
    SEED_CONFERENCE_SOURCES,
    SEED_CONFERENCE_URLS,
    SEED_CONFERENCES,
    WEEKLY_SUBCATEGORIES,
    monthly_subcategories,
    seed_subcategories,
    weekly_subcategories,
)
from conference_agent.discover import (
    _attendance_hints_block,
    _ExtractedConference,
    _parse_date,
    _seed_checklist,
    _to_conference,
)
from conference_agent.models import (
    Conference,
    ConferenceSize,
    RemoteOption,
    normalize_subcategories,
)


def _extracted(**overrides):
    base = {f: "" for f in _ExtractedConference.model_fields}
    base.update(acronym="rsna", name="RSNA Annual Meeting", subcategory="Radiology")
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
            attendance="45,000",
            attendance_year="2025",
            attendance_source="https://www.rsna.org/annual-meeting/attendance",
            remote_option="Hybrid",
            cost="$1,095",
        )
    )
    assert conf is not None
    assert conf.id == "RSNA"
    assert conf.subcategory == "radiology"  # normalized to lowercase
    assert conf.category == "medicine"  # derived from the subcategory
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.location == "Chicago, IL"
    # Attendance is parsed (commas stripped); size is derived from it.
    assert conf.attendance == 45000
    assert conf.attendance_year == 2025
    assert conf.attendance_source == "https://www.rsna.org/annual-meeting/attendance"
    assert conf.size == ConferenceSize.LARGE
    assert conf.remote_option == RemoteOption.HYBRID
    assert conf.cost == "$1,095"


def test_to_conference_parses_multiple_subcategories():
    conf = _to_conference(
        _extracted(acronym="miccai", name="MICCAI", subcategory="Radiology, Machine Learning")
    )
    assert conf is not None
    assert conf.subcategories == ["radiology", "machine learning"]
    assert conf.subcategory == "radiology, machine learning"
    # The broad category is derived from those subcategories.
    assert conf.categories == ["medicine", "artificial intelligence"]


def test_to_conference_parses_formats():
    conf = _to_conference(
        _extracted(acronym="neurips", name="NeurIPS", formats="Paper, Poster, Oral")
    )
    assert conf is not None
    # Normalized to the canonical abstract/paper/poster/oral order; unknown tokens
    # would be dropped by normalize_formats.
    assert conf.formats == ["paper", "poster", "oral"]
    assert conf.format == "paper, poster, oral"
    # No formats stated -> empty list (the _extracted base leaves it "").
    assert _to_conference(_extracted(acronym="abc", name="Some Conf")).formats == []


def test_size_is_derived_from_extracted_attendance():
    # Size follows the attendance figure deterministically, not a model label.
    big = _to_conference(_extracted(acronym="ecr", name="European Congress of Radiology", attendance="30000"))
    assert big.size == ConferenceSize.LARGE

    mid = _to_conference(_extracted(acronym="spr", name="Society for Pediatric Radiology", attendance="500"))
    assert mid.size == ConferenceSize.MEDIUM

    small = _to_conference(_extracted(acronym="abc", name="Some Conf", attendance="50"))
    assert small.size == ConferenceSize.SMALL


def test_to_conference_blank_optionals_become_none():
    conf = _to_conference(_extracted(acronym="abc", name="Some Conf"))
    assert conf.upcoming_start_date is None
    assert conf.attendance is None
    assert conf.size is None
    assert conf.remote_option is None
    assert conf.cost is None


def test_to_conference_invalid_enum_and_date_are_dropped():
    conf = _to_conference(
        _extracted(
            acronym="abc",
            name="Some Conf",
            attendance="lots of people",
            remote_option="telepathic",
            upcoming_start_date="2026-13-40",
        )
    )
    # An unparseable attendance is dropped, so size stays blank.
    assert conf.attendance is None
    assert conf.size is None
    assert conf.remote_option is None
    assert conf.upcoming_start_date is None


def test_to_conference_requires_identity_fields():
    assert _to_conference(_extracted(acronym="")) is None
    assert _to_conference(_extracted(name="")) is None


def test_seed_list_is_well_formed():
    # Each seed is a (acronym, name, subcategory) tuple. The subcategory element is
    # a string or a tuple of strings (a conference may span several fields).
    from conference_agent.models import SUBCATEGORY_TO_CATEGORY

    assert SEED_CONFERENCES
    for acronym, name, subcategory in SEED_CONFERENCES:
        subs = normalize_subcategories(subcategory)
        assert acronym.strip() and name.strip() and subs
        # Seeds must satisfy the same identity/validation rules as discovered rows.
        conf = Conference(acronym=acronym, name=name, subcategory=subcategory)
        assert conf.id == acronym.upper()
        assert conf.subcategories == subs
        # Every seed subcategory must be mapped so its broad category derives.
        for sub in subs:
            assert sub in SUBCATEGORY_TO_CATEGORY, f"{acronym}: '{sub}' is unmapped"
        assert conf.categories  # a derived category is always present
        # A seed carries no attendance, so its size is blank until discovery.
        assert conf.size is None

    # Acronyms are the upsert key, so they must be unique (case-insensitive).
    acronyms = [a.upper() for a, _, _ in SEED_CONFERENCES]
    assert len(acronyms) == len(set(acronyms))


def test_multi_tag_and_cshl_genomics_seeds():
    by_id = {a.upper(): normalize_subcategories(c) for a, _, c in SEED_CONFERENCES}
    # Conferences that span fields carry every applicable tag.
    assert by_id["SPR"] == ["radiology", "pediatrics"]
    assert by_id["MICCAI"] == ["radiology", "machine learning"]
    assert "ECCV" in by_id  # newly added computer-vision flagship
    # Every CSHL meeting carries a genomics tag (its home domain).
    cshl = {a: subs for a, subs in by_id.items() if a.startswith("CSHL-")}
    assert cshl
    for acronym, subs in cshl.items():
        assert "genomics" in subs, f"{acronym} missing genomics tag"


def test_attendance_hints_block_renders_sources_and_bump_instruction():
    # No hints -> empty string, so a first-time run's prompt is unchanged.
    assert _attendance_hints_block(None) == ""
    assert _attendance_hints_block({}) == ""

    block = _attendance_hints_block(
        {
            "RSNA": {"source": "https://rsna.org/2024/by-the-numbers", "year": 2024},
            "SIIM": {"source": "https://siim.org/attendance", "year": None},
        }
    )
    # Lists each source, with the year when known, and tells the model to reuse the
    # URL first and bump a year in the URL on a refresh.
    assert "https://rsna.org/2024/by-the-numbers" in block
    assert "RSNA (2024)" in block
    assert "SIIM:" in block  # no year -> no parenthetical
    assert "advanced to the next edition" in block
    # An entry without a usable source URL is skipped.
    assert _attendance_hints_block({"X": {"source": "", "year": 2025}}) == ""


def test_seed_checklist_includes_seeds_and_filters_by_subcategory():
    # The checklist for a subcategory lists exactly that field's seeds. Each line
    # is rendered as "- {acronym} — {name}", so match on that exact prefix rather
    # than a bare substring (e.g. so "ASH" does not match inside "ASHNR").
    checklist = _seed_checklist(["radiology"])
    for acronym, _, subcategory in SEED_CONFERENCES:
        line = f"- {acronym} — "
        if "radiology" in normalize_subcategories(subcategory):
            assert line in checklist
        else:
            assert line not in checklist
    # A multi-tag seed appears in every field it is tagged with: MICCAI is both
    # radiology and machine learning, so it shows up in both checklists.
    assert "- MICCAI — " in _seed_checklist(["radiology"])
    assert "- MICCAI — " in _seed_checklist(["machine learning"])
    # A subcategory with no seeds yields the explicit empty-state line.
    assert "no seeds" in _seed_checklist(["underwater basket weaving"]).lower()


def test_seed_sources_are_well_formed():
    assert SEED_CONFERENCE_SOURCES
    for label, url, note in SEED_CONFERENCE_SOURCES:
        assert label.strip() and note.strip()
        assert url.startswith("https://")


def test_cadence_partitions_the_seed_subcategories():
    weekly = weekly_subcategories()
    monthly = monthly_subcategories()
    # Weekly and monthly are disjoint and together cover every seeded subcategory.
    assert set(weekly).isdisjoint(monthly)
    assert sorted(weekly + monthly) == seed_subcategories()
    # Weekly is exactly the seeded subcategories named in WEEKLY_SUBCATEGORIES.
    assert set(weekly) == WEEKLY_SUBCATEGORIES & set(seed_subcategories())


def test_weekly_subcategories_are_all_real_subcategories():
    # Every name in WEEKLY_SUBCATEGORIES must correspond to an actual seed
    # subcategory, otherwise the weekly job would silently refresh nothing for it.
    assert WEEKLY_SUBCATEGORIES <= set(seed_subcategories())


def test_mlcb_seed_present_in_genomics_with_url():
    entry = [s for s in SEED_CONFERENCES if s[0] == "MLCB"]
    assert entry, "MLCB seed missing"
    assert "genomics" in normalize_subcategories(entry[0][2])
    assert SEED_CONFERENCE_URLS.get("MLCB") == "https://www.mlcb.org"


# --- Backend dispatch (hermetic; no network/LLM) ---------------------------


def test_discover_rejects_unknown_backend():
    with pytest.raises(ValueError):
        discover.discover_conferences(subcategories=["radiology"], backend="bogus")


def test_claude_code_backend_dispatches_without_api_key(monkeypatch):
    # The default backend must not require ANTHROPIC_API_KEY or touch the SDK.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(discover, "_research_via_cli", lambda subs, model, hints=None: "notes")
    sentinel = [object()]
    monkeypatch.setattr(discover, "_extract_via_cli", lambda text, model: sentinel)
    out = discover.discover_conferences(subcategories=["genomics"], backend="claude-code")
    assert out is sentinel


def test_claude_code_backend_returns_empty_when_no_research(monkeypatch):
    monkeypatch.setattr(discover, "_research_via_cli", lambda subs, model, hints=None: "   ")
    out = discover.discover_conferences(subcategories=["genomics"], backend="claude-code")
    assert out == []


class _FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_run_claude_cli_strips_api_key_and_builds_command(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeProc(json.dumps({"is_error": False, "result": "ok", "structured_output": {"a": 1}}))

    monkeypatch.setattr(discover.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(discover.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

    payload = discover._run_claude_cli(
        "prompt",
        append_system="sys",
        tools=["WebSearch", "WebFetch"],
        json_schema={"type": "object"},
        model="opus",
        timeout=10,
    )
    assert payload["result"] == "ok"
    # The subprocess must not inherit the API key, so the CLI uses the subscription.
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    cmd = captured["cmd"]
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    for flag in ("--output-format", "--append-system-prompt", "--tools", "--allowedTools", "--json-schema", "--model"):
        assert flag in cmd


def test_run_claude_cli_no_allowedtools_when_tools_empty(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env):
        captured["cmd"] = cmd
        return _FakeProc(json.dumps({"is_error": False, "structured_output": {}}))

    monkeypatch.setattr(discover.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(discover.subprocess, "run", fake_run)
    discover._run_claude_cli("prompt", tools=[], timeout=10)
    # An empty tool list disables tools; it must not pre-approve any with --allowedTools.
    assert "--tools" in captured["cmd"]
    assert "--allowedTools" not in captured["cmd"]


def test_run_claude_cli_raises_on_error_payload(monkeypatch):
    monkeypatch.setattr(discover.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        discover.subprocess,
        "run",
        lambda *a, **k: _FakeProc(json.dumps({"is_error": True, "result": "boom"})),
    )
    with pytest.raises(RuntimeError, match="boom"):
        discover._run_claude_cli("prompt", tools=[], timeout=10)


def test_run_claude_cli_raises_when_cli_missing(monkeypatch):
    monkeypatch.setattr(discover.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="claude"):
        discover._run_claude_cli("prompt", tools=[], timeout=10)
