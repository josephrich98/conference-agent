"""Offline tests for the Abstract / Paper guidelines columns.

Covers the text cleanup, the write paths (a survey keeps stored guidelines; a
manual edit sets them), the guidelines pass's due policy and storage, and
``conference-agent discover --guidelines`` with the agent call replaced.
"""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from conference_agent import discover
from conference_agent.cli import main
from conference_agent.database import (
    ConferenceRow,
    get_engine,
    guidelines_targets,
    merge_records,
    query_conferences,
    record_guidelines,
    set_pins,
    upsert_conferences,
)
from conference_agent.models import Conference, name_id, normalize_guidelines

TODAY = date(2026, 10, 30)


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _conf(name, deadline=None, **extra):
    return Conference(acronym=name.split()[0], name=name, subcategory="radiology",
                      upcoming_abstract_deadline=deadline, **extra)


def test_normalize_guidelines_cleans_whitespace():
    assert normalize_guidelines("  - a  \r\n\r\n\r\n- b\n ") == "- a\n\n- b"
    assert normalize_guidelines(" \n ") is None
    assert normalize_guidelines(None) is None


def test_survey_keeps_stored_guidelines_and_manual_edit_sets_them(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf("Alpha Meeting", abstract_guidelines="- 300 words")], db_url=url)
    # A survey does not research guidelines, so its record carries none.
    upsert_conferences([_conf("Alpha Meeting", TODAY)], db_url=url)
    (conf,) = query_conferences(db_url=url)
    assert conf.abstract_guidelines == "- 300 words"
    merge_records([{"name": "Alpha Meeting", "paper_guidelines": "8 pages\r\n"}], db_url=url)
    (conf,) = query_conferences(db_url=url)
    assert conf.paper_guidelines == "8 pages"


def test_targets_need_a_deadline_ahead_and_follow_the_recheck_policy(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences(
        [
            _conf("Past Meeting", TODAY - timedelta(days=1)),
            _conf("Soon Meeting", TODAY + timedelta(days=5)),
            _conf("Later Meeting", TODAY + timedelta(days=50)),
            _conf("Undated Meeting"),
        ],
        db_url=url,
    )
    # Never researched: due, soonest deadline first.
    assert [c.name for c in guidelines_targets(url, TODAY)] == ["Soon Meeting", "Later Meeting"]

    record_guidelines({name_id("Soon Meeting"): {"abstract_guidelines": "250 words"}},
                      [name_id("Soon Meeting"), name_id("Later Meeting")], db_url=url, today=TODAY)
    assert guidelines_targets(url, TODAY) == []
    # Nothing found: retried after 14 days. Found: re-researched after 120.
    assert [c.name for c in guidelines_targets(url, TODAY + timedelta(days=14))] == ["Later Meeting"]
    assert guidelines_targets(url, TODAY, force=True)[0].name == "Soon Meeting"
    (soon,) = [c for c in query_conferences(db_url=url) if c.name == "Soon Meeting"]
    assert soon.abstract_guidelines == "250 words"


def test_record_guidelines_keeps_stored_text_on_blank_and_respects_pins(tmp_path):
    url = _db_url(tmp_path)
    upsert_conferences([_conf("Alpha Meeting", TODAY, abstract_guidelines="old",
                              paper_guidelines="old paper")], db_url=url)
    row_id = name_id("Alpha Meeting")
    set_pins(row_id, pin=["paper_guidelines"], db_url=url)
    changed = record_guidelines({row_id: {"abstract_guidelines": "", "paper_guidelines": "new"}},
                                [row_id], db_url=url, today=TODAY)
    assert changed == 0
    with Session(get_engine(url)) as session:
        row = session.get(ConferenceRow, row_id)
        assert (row.abstract_guidelines, row.paper_guidelines) == ("old", "old paper")
        assert row.guidelines_checked == TODAY


def test_discover_guidelines_researches_due_series(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    ahead = date.today() + timedelta(days=10)
    upsert_conferences([_conf("Alpha Meeting", ahead), _conf("Beta Meeting")], db_url=url)
    seen = []

    def fake_research(targets, **_):
        seen.append([t.name for t in targets])
        return {t.id: {"abstract_guidelines": "- 200 words", "paper_guidelines": None}
                for t in targets}

    monkeypatch.setattr(discover, "research_guidelines", fake_research)
    assert main(["--db", url, "discover", "--guidelines"]) == 0
    assert seen == [["Alpha Meeting"]]
    by_name = {c.name: c for c in query_conferences(db_url=url)}
    assert by_name["Alpha Meeting"].abstract_guidelines == "- 200 words"
    # Researched today, so a second run has nothing due.
    assert main(["--db", url, "discover", "--guidelines"]) == 0
    assert len(seen) == 1


def test_add_accepts_guidelines_flags(tmp_path):
    url = _db_url(tmp_path)
    assert main(["--db", url, "add", "--conference-name", "Gamma Meeting",
                 "--subcategory", "radiology",
                 "--abstract-guidelines", "- 300 words\n- Structured",
                 "--paper-guidelines", "8 pages"]) == 0
    (conf,) = query_conferences(db_url=url)
    assert conf.abstract_guidelines == "- 300 words\n- Structured"
    assert conf.paper_guidelines == "8 pages"
