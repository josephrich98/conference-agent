"""Offline tests for ``conference-agent discover`` field selection and ``--options``.

The agent calls (``discover_conferences`` / ``refresh_conferences``) are replaced
with recorders, so these check which fields or series a run would research
without network access or an API key.
"""

import pytest

from conference_agent import discover
from conference_agent.cli import main
from conference_agent.config import seed_subcategories
from conference_agent.database import upsert_conferences
from conference_agent.models import CATEGORIES, Conference


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def calls(monkeypatch):
    """Record each agent call instead of running it; both return no records."""
    seen = {"survey": [], "refresh": []}

    def fake_discover(subcategories=None, **_):
        seen["survey"].append(list(subcategories))
        return []

    def fake_refresh(targets, **_):
        seen["refresh"].append([t.name for t in targets])
        return []

    monkeypatch.setattr(discover, "discover_conferences", fake_discover)
    monkeypatch.setattr(discover, "refresh_conferences", fake_refresh)
    return seen


def _store(url):
    upsert_conferences(
        [
            Conference(acronym="ZZA", name="Zeta Imaging Meeting", subcategory="radiology",
                       attendance=20000),
            Conference(acronym="ZZB", name="Zeta Optics Workshop", subcategory="optics",
                       attendance=100),
        ],
        db_url=url,
    )


def test_bare_discover_surveys_every_field(tmp_path, calls):
    url = _db_url(tmp_path)
    _store(url)
    assert main(["--db", url, "discover"]) == 0
    surveyed = [fields[0] for fields in calls["survey"]]
    assert all(len(fields) == 1 for fields in calls["survey"])  # one run per field
    assert set(surveyed) == set(seed_subcategories()) | {"radiology", "optics"}
    assert calls["refresh"] == []


def test_category_expands_to_its_subcategories(tmp_path, calls):
    url = _db_url(tmp_path)
    _store(url)
    assert main(["--db", url, "discover", "--category", "physics"]) == 0
    surveyed = {fields[0] for fields in calls["survey"]}
    assert "optics" in surveyed
    assert "radiology" not in surveyed


def test_size_rechecks_matching_stored_series_only(tmp_path, calls):
    url = _db_url(tmp_path)
    _store(url)
    assert main(["--db", url, "discover", "--size", "massive"]) == 0
    assert calls["refresh"] == [["Zeta Imaging Meeting"]]
    assert calls["survey"] == []


def test_conference_name_is_case_insensitive_and_narrowed_by_category(tmp_path, calls):
    url = _db_url(tmp_path)
    _store(url)
    assert main(["--db", url, "discover", "--conference-name", "zeta imaging  MEETING"]) == 0
    assert calls["refresh"] == [["Zeta Imaging Meeting"]]

    calls["refresh"].clear()
    args = ["--db", url, "discover", "--conference-name", "Zeta Imaging Meeting",
            "--category", "physics"]
    assert main(args) == 0
    assert calls["refresh"] == []


def test_unknown_conference_name_fails(tmp_path, calls, capsys):
    url = _db_url(tmp_path)
    _store(url)
    assert main(["--db", url, "discover", "--conference-name", "No Such Meeting"]) == 1
    assert "list --names" in capsys.readouterr().err
    assert calls["refresh"] == []


def test_options_lists_valid_values(tmp_path, calls, capsys):
    url = _db_url(tmp_path)
    _store(url)
    assert main(["--db", url, "discover", "--options"]) == 0
    out = capsys.readouterr().out
    for flag in ("--conference-name", "--category", "--subcategory", "--size"):
        assert flag in out
    assert all(f"  {c}\n" in out for c in CATEGORIES)
    assert "  optics" in out and "  massive\n" in out
    assert "list --names" in out
    assert calls == {"survey": [], "refresh": []}


def test_list_names(tmp_path, capsys):
    url = _db_url(tmp_path)
    _store(url)
    assert main(["--db", url, "list", "--names"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Zeta Imaging Meeting", "Zeta Optics Workshop"]


def test_discover_conferences_requires_a_field():
    with pytest.raises(ValueError, match="discovery_subcategories"):
        discover.discover_conferences(subcategories=None)
