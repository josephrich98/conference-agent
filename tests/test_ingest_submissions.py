"""``scripts/ingest_submissions.py``: merged form submissions -> the database.

The git side is replaced by a stub, so these run offline in any checkout.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ingest_submissions as ing  # noqa: E402

from conference_agent.database import query_conferences  # noqa: E402

RECORD = {"conference_name": "Submitted Test Conference", "subcategory": "genomics",
          "abstract_due": "2099-03-01"}


def _stub_git(monkeypatch, files):
    def git(*args):
        if args[0] == "fetch":
            return ""
        if args[0] == "ls-tree":
            return "\n".join(files) + "\n"
        if args[0] == "show":
            return files[args[1].split(":", 1)[1]]
        raise AssertionError(args)

    monkeypatch.setattr(ing, "_git", git)


def _main(monkeypatch, tmp_path, url):
    state = tmp_path / "state.json"
    monkeypatch.setattr(sys, "argv", ["ingest", "--db", url, "--state", str(state)])
    return ing.main(), json.loads(state.read_text()) if state.exists() else {}


def test_new_submissions_are_added_once(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    path = "submissions/submitted-test-conference-2099-01-01-abc.json"
    _stub_git(monkeypatch, {path: json.dumps(RECORD)})
    code, state = _main(monkeypatch, tmp_path, url)
    assert code == 0 and state[path]["ok"] is True
    assert [c.name for c in query_conferences(db_url=url)] == ["Submitted Test Conference"]
    # Already processed: not added again (which would fail on the existing name).
    code, _ = _main(monkeypatch, tmp_path, url)
    assert code == 0


def test_failed_submission_is_recorded_and_not_retried(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    first, dup = "submissions/a.json", "submissions/b.json"
    _stub_git(monkeypatch, {first: json.dumps(RECORD), dup: json.dumps(RECORD)})
    code, state = _main(monkeypatch, tmp_path, url)
    assert code == 1
    assert state[first]["ok"] is True
    assert state[dup]["ok"] is False and "already exists" in state[dup]["output"]
    code, _ = _main(monkeypatch, tmp_path, url)
    assert code == 0


def test_update_submission_changes_only_its_fields(tmp_path, monkeypatch):
    """An edit from the form (``*.update.json``) runs ``add --update``."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    add, edit = "submissions/a.json", "submissions/b.update.json"
    _stub_git(monkeypatch, {
        add: json.dumps(RECORD),
        edit: json.dumps({"conference_name": RECORD["conference_name"], "attendance": 1200}),
    })
    code, state = _main(monkeypatch, tmp_path, url)
    assert code == 0 and state[edit]["ok"] is True
    [conf] = query_conferences(db_url=url)
    assert conf.attendance == 1200
    assert str(conf.upcoming_abstract_deadline) == RECORD["abstract_due"]
