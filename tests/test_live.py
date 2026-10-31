"""Offline tests for ``conference_agent.live`` (lookup / info over the live snapshot)."""

import json

import pytest

from conference_agent import cli, live

_SNAPSHOT = {
    "generated": "2099-01-01",
    "conferences": [
        {"id": "zeta-imaging-meeting", "acronym": "ZZA", "name": "Zeta Imaging Meeting",
         "subcategory": "radiology", "size": "massive", "attendance": 20000,
         "upcoming_start_date": "2099-11-01", "stable_location": False,
         "location_venue": False, "not_a_column": 1},
        {"id": "zeta-optics-workshop", "acronym": "ZZB", "name": "Zeta Optics Workshop",
         "subcategory": "optics", "size": "small", "attendance": 100,
         "upcoming_start_date": "2099-03-01", "upcoming_location": "Oslo, Norway"},
    ],
}


class _Response:
    def __init__(self, body, etag='"v1"'):
        self.body, self.headers = body, {"ETag": etag}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Serve ``_SNAPSHOT`` in place of the live site; record request headers."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    state = {"body": json.dumps(_SNAPSHOT).encode(), "error": None, "headers": []}

    def urlopen(request, timeout):
        state["headers"].append(dict(request.header_items()))
        if state["error"]:
            raise state["error"]
        return _Response(state["body"])

    monkeypatch.setattr(live.urllib.request, "urlopen", urlopen)
    return state


def test_lookup_reads_live_snapshot(capsys, served):
    assert cli.main(["lookup", "--columns", "acronym", "--query", "size:massive"]) == 0
    assert capsys.readouterr().out.splitlines() == ["ZZA"]


def test_info_reads_live_snapshot(capsys, served):
    assert cli.main(["info", "ZZB", "upcoming_start_date", "location"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "upcoming_start_date: 2099-03-01",
        "location: Oslo, Norway",
    ]


def test_revalidates_with_etag_and_falls_back_to_cache(capsys, served):
    first = live.live_database_url()
    served["error"] = live.urllib.error.URLError("offline")
    assert live.live_database_url(refresh=True) == first
    assert served["headers"][-1].get("If-none-match") == '"v1"'
    assert "using the cached copy" in capsys.readouterr().err


def test_unreachable_without_cache_errors(capsys, served):
    served["error"] = live.urllib.error.URLError("offline")
    assert cli.main(["lookup", "--columns", "acronym"]) == 1
    assert "--local" in capsys.readouterr().err


def test_local_flag_skips_live(capsys, served, tmp_path):
    db = f"sqlite:///{tmp_path / 'local.db'}"
    assert cli.main(["--db", db, "lookup", "--columns", "acronym"]) == 0
    assert served["headers"] == []


def test_checks_site_once_a_day(served, tmp_path):
    first = live.live_database_url()
    assert live.live_database_url() == first
    assert len(served["headers"]) == 1  # the second call used the cache
    (tmp_path / "conference-agent" / "conferences.checked").write_text("2000-01-01")
    live.live_database_url()
    assert len(served["headers"]) == 2  # a new day revalidates


def test_refresh_flag_forces_check(capsys, served):
    assert cli.main(["info", "ZZB", "acronym"]) == 0
    assert cli.main(["info", "ZZB", "acronym"]) == 0
    assert len(served["headers"]) == 1
    assert cli.main(["info", "ZZB", "acronym", "--refresh"]) == 0
    assert len(served["headers"]) == 2


def test_failed_check_is_retried(capsys, served):
    live.live_database_url()
    (live.cache_dir() / "conferences.checked").write_text("2000-01-01")
    served["error"] = live.urllib.error.URLError("offline")
    live.live_database_url()
    live.live_database_url()
    assert len(served["headers"]) == 3  # no same-day stamp after a failure
