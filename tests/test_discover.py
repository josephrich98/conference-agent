"""Offline tests for the discovery extraction/conversion helpers.

These cover the flat-string -> typed ``Conference`` mapping (the step that turns
the LLM's structured output into validated records). No network or LLM calls.
"""

import json
from datetime import date

import pytest

import conference_agent.discover as discover
from conference_agent.config import (
    WEEKLY_SUBCATEGORIES,
    monthly_subcategories,
    weekly_subcategories,
)
from conference_agent.discover import (
    _attendance_hints_block,
    _ExtractedConference,
    _known_checklist,
    _parse_date,
    _to_conference,
)
from conference_agent.models import (
    Conference,
    ConferenceSize,
    RemoteOption,
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
            upcoming_location="Chicago, IL",
            attendance="45,000",
            attendance_year="2025",
            attendance_source="https://www.rsna.org/annual-meeting/attendance",
            remote_option="Hybrid",
            upcoming_cost="$1,095",
        )
    )
    assert conf is not None
    assert conf.id == "rsna-annual-meeting"
    assert conf.subcategory == "radiology"  # normalized to lowercase
    assert conf.category == ""  # none extracted; category is not derived
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.location == "Chicago, IL"
    # Attendance is parsed (commas stripped); size is derived from it.
    assert conf.attendance == 45000
    assert conf.attendance_year == 2025
    assert conf.attendance_source == "https://www.rsna.org/annual-meeting/attendance"
    assert conf.size == ConferenceSize.MASSIVE
    assert conf.remote_option == RemoteOption.HYBRID
    assert conf.cost == "$1,095"


def test_to_conference_parses_multiple_subcategories():
    conf = _to_conference(
        _extracted(acronym="miccai", name="MICCAI", subcategory="Radiology, Machine Learning",
                   category="Artificial Intelligence, Medicine")
    )
    assert conf is not None
    assert conf.subcategories == ["radiology", "machine learning"]
    assert conf.subcategory == "radiology, machine learning"
    # The broad categories are extracted alongside, in canonical order.
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
    assert big.size == ConferenceSize.MASSIVE

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


def test_known_checklist_lists_stored_conferences_in_the_field():
    known = [
        Conference(acronym="MICCAI", name="Medical Image Computing",
                   subcategory="radiology, machine learning"),
        Conference(acronym="ASH", name="American Society of Hematology", subcategory="hematology"),
    ]
    # Each line is "- {acronym} — {name}"; a multi-tag series appears in every field.
    assert "- MICCAI — Medical Image Computing" in _known_checklist(["radiology"], known)
    assert "- MICCAI — " in _known_checklist(["machine learning"], known)
    assert "- ASH — " not in _known_checklist(["radiology"], known)
    # A field with nothing stored yields the explicit empty-state line.
    assert "none recorded" in _known_checklist(["origami"], known)
    assert "none recorded" in _known_checklist(["radiology"], None)


def test_cadence_partitions_the_given_fields():
    fields = ["radiology", "genomics", "origami", "pediatrics"]
    weekly = weekly_subcategories(fields)
    monthly = monthly_subcategories(fields)
    assert set(weekly).isdisjoint(monthly)
    assert sorted(weekly + monthly) == sorted(fields)
    assert set(weekly) == WEEKLY_SUBCATEGORIES & set(fields)
def test_discover_rejects_unknown_backend():
    with pytest.raises(ValueError):
        discover.discover_conferences(subcategories=["radiology"], backend="bogus")


def test_claude_code_backend_dispatches_without_api_key(monkeypatch):
    # The default backend must not require ANTHROPIC_API_KEY or touch the SDK.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(discover, "_research_via_cli", lambda subs, model, hints=None, known=None: "notes")
    sentinel = [object()]
    monkeypatch.setattr(discover, "_extract_via_cli", lambda text, model: sentinel)
    out = discover.discover_conferences(subcategories=["genomics"], backend="claude-code")
    assert out is sentinel


def test_claude_code_backend_returns_empty_when_no_research(monkeypatch):
    monkeypatch.setattr(discover, "_research_via_cli", lambda subs, model, hints=None, known=None: "   ")
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


def test_refresh_conferences_targets_named_series_only(monkeypatch):
    from datetime import date

    from conference_agent.models import Conference

    target = Conference(acronym="RSNA", name="Radiological Society of North America",
                        subcategory="radiology", url="https://www.rsna.org/annual-meeting",
                        upcoming_abstract_deadline=date(2026, 5, 6))
    seen = {}

    def fake_research(system, prompt, model):
        seen["system"], seen["prompt"] = system, prompt
        return "notes"

    monkeypatch.setattr(discover, "_research_text_via_cli", fake_research)
    monkeypatch.setattr(
        discover,
        "_extract_via_cli",
        lambda text, model: [
            Conference(acronym="RSNA", name="RSNA", subcategory="radiology"),
            Conference(acronym="OTHER", name="Other", subcategory="radiology"),
        ],
    )
    found = discover.refresh_conferences([target])
    # The re-worded "RSNA" record matches the target by acronym and comes back
    # under the stored name, so it merges into the same row.
    assert [c.id for c in found] == [target.id]
    assert found[0].name == target.name
    assert "Re-check ONLY" in seen["prompt"]
    assert "https://www.rsna.org/annual-meeting" in seen["prompt"]
    assert "abstract 2026-05-06" in seen["prompt"]
    assert "- RSNA — Radiological Society of North America" in seen["system"]


def test_refresh_conferences_shared_acronym_matches_by_name_only(monkeypatch):
    from conference_agent.models import Conference

    a = Conference(acronym="ISMB", name="Intelligent Systems for Molecular Biology", subcategory="bioinformatics")
    b = Conference(acronym="ISMB", name="International Society for Magnetic Bodies", subcategory="physics")
    monkeypatch.setattr(discover, "_research_text_via_cli", lambda *args: "notes")
    monkeypatch.setattr(
        discover,
        "_extract_via_cli",
        lambda text, model: [
            # Two targets share the acronym, so a re-worded name matches neither.
            Conference(acronym="ISMB", name="ISMB 2027", subcategory="bioinformatics"),
            Conference(acronym="ISMB", name="International Society for Magnetic Bodies",
                       subcategory="physics", upcoming_start_date=date(2027, 1, 1)),
        ],
    )
    found = discover.refresh_conferences([a, b])
    assert [c.id for c in found] == [b.id]


def test_refresh_conferences_empty_targets_skip_the_agent(monkeypatch):
    monkeypatch.setattr(discover, "_research_text_via_cli", lambda *a: pytest.fail("called"))
    assert discover.refresh_conferences([]) == []


def test_to_conference_carries_structured_deadline_times_and_registration():
    # These fields are requested by the research prompt; extraction must carry
    # them through (they were once missing from the schema and silently dropped).
    conf = _to_conference(
        _extracted(
            acronym="iclr",
            name="ICLR",
            abstract_time="11:59 PM",
            abstract_timezone="Eastern Time",
            paper_time="23:59",
            paper_timezone="AoE",
            upcoming_registration="Early bird: Jan 5 - Mar 1",
            prior_registration="Opened Feb 2026",
        )
    )
    # Normalized on the way in: 24-hour time, canonical zone code.
    assert (conf.abstract_time, conf.abstract_timezone) == ("23:59", "ET")
    assert (conf.paper_time, conf.paper_timezone) == ("23:59", "AoE")
    assert conf.deadline_time == "abstract: 11:59 PM ET\npaper: 11:59 PM AoE"
    assert conf.upcoming_registration == "Early bird: Jan 5 - Mar 1"
    assert conf.prior_registration == "Opened Feb 2026"
    # Unstated -> None, so a fill-only refresh merge never blanks a stored value.
    blank = _to_conference(_extracted(acronym="abc", name="Some Conf"))
    assert blank.deadline_time is None and blank.abstract_time is None and blank.upcoming_registration is None
