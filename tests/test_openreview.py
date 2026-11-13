"""Offline tests for the OpenReview sync (``openreview_source``).

A fake client stands in for the API, serving venue groups and Submission
invitations in the shapes ``api2.openreview.net`` returns.
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.orm import Session

from conference_agent.database import (
    ConferenceRow,
    get_engine,
    set_pins,
    upsert_conferences,
)
from conference_agent.models import Conference
from conference_agent.openreview_source import (
    NO_LINK,
    Edition,
    candidate_templates,
    edition_from_api,
    local_deadline,
    parse_start_date,
    parse_summary,
    plan_update,
    set_link,
    sync,
    title_matches,
    venue_templates,
)
from conference_agent.page_watch import PageCheck
from conference_agent.refresh import run_watch

TODAY = date(2026, 8, 1)


def _ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def _group(title, website, start="Apr 26 2027", summary="", location="Rio, Brazil", **extra):
    content = {
        "title": title,
        "subtitle": title,
        "website": website,
        "start_date": start,
        "location": location,
        "date": summary,
        **extra,
    }
    return {"content": {k: {"value": v} for k, v in content.items()}}


class FakeClient:
    def __init__(self, groups=None, invitations=None, venues=None):
        self.groups = groups or {}
        self.invitations = invitations or {}
        self.venues = venues if venues is not None else [
            vid for vid in self.groups if vid.endswith("/Conference")
        ]
        self.requests = 0

    def venue_ids(self):
        return self.venues

    def group(self, group_id):
        self.requests += 1
        return self.groups.get(group_id)

    def invitation(self, invitation_id):
        self.requests += 1
        return self.invitations.get(invitation_id)


ICLR_SUMMARY = (
    "Submission Start: Aug 01 2026 11:59AM UTC-0, Abstract Registration: "
    "Sep 19 2026 11:59AM UTC-0, Submission Deadline: Sep 25 2026 11:59AM UTC-0"
)


def _iclr_client(due=None, summary=ICLR_SUMMARY):
    vid = "ICLR.cc/2027/Conference"
    return FakeClient(
        groups={vid: _group("International Conference on Learning Representations",
                            "https://iclr.cc/Conferences/2027", summary=summary)},
        invitations={f"{vid}/-/Submission": {"duedate": due or _ms(2026, 9, 19, 11, 59)}},
    )


def _db(tmp_path, **fields):
    url = f"sqlite:///{tmp_path / 'c.db'}"
    base = dict(acronym="ICLR", name="International Conference on Learning Representations",
                subcategory="machine learning", url="https://iclr.cc",
                prior_abstract_deadline=date(2025, 9, 19),
                prior_paper_deadline=date(2025, 9, 24),
                prior_start_date=date(2026, 4, 23), abstract_time="23:59",
                abstract_timezone="AoE", paper_time="23:59", paper_timezone="AoE")
    base.update(fields)
    upsert_conferences([Conference(**base)], db_url=url)
    return url


def _row(url, row_id="international-conference-on-learning-representations"):
    with Session(get_engine(url)) as session:
        row = session.get(ConferenceRow, row_id)
        session.expunge(row)
        return row


# --- Parsing --------------------------------------------------------------------


def test_parse_summary_reads_the_deadlines_in_utc():
    out = parse_summary(ICLR_SUMMARY)
    assert out["abstract"] == datetime(2026, 9, 19, 11, 59, tzinfo=timezone.utc)
    assert out["paper"] == datetime(2026, 9, 25, 11, 59, tzinfo=timezone.utc)
    shifted = parse_summary("Submission Deadline: May 07 2026 11:59PM UTC+2")
    assert shifted["paper"] == datetime(2026, 5, 7, 21, 59, tzinfo=timezone.utc)
    assert parse_summary("") == {}


def test_parse_start_date_text_and_millis():
    assert parse_start_date("Apr 23 2026") == date(2026, 4, 23)
    assert parse_start_date(_ms(2027, 6, 20)) == date(2027, 6, 20)
    assert parse_start_date(None) is None and parse_start_date("TBA") is None


def test_edition_uses_the_live_invitation_and_the_summary():
    group = _group("X", "https://x.org", summary=ICLR_SUMMARY)
    # An extended abstract deadline: the invitation moved, the summary did not.
    ed = edition_from_api("X/2027/Conference", 2027, group, {"duedate": _ms(2026, 9, 21, 11, 59)})
    assert ed.abstract == datetime(2026, 9, 21, 11, 59, tzinfo=timezone.utc)
    assert ed.paper == datetime(2026, 9, 25, 11, 59, tzinfo=timezone.utc)
    # The same instant listed twice is one (paper) deadline.
    same = "Abstract Registration: Aug 29 2026 01:00PM UTC-0, Submission Deadline: Aug 29 2026 01:00PM UTC-0"
    ed = edition_from_api("X/2027/Conference", 2027, _group("X", "x", summary=same), None)
    assert ed.abstract is None and ed.paper is not None
    # No summary: the invitation's deadline is unlabeled.
    ed = edition_from_api("X/2027/Conference", 2027, _group("X", "x"), {"duedate": _ms(2026, 12, 4, 11, 59)})
    assert (ed.abstract, ed.paper, ed.single.date()) == (None, None, date(2026, 12, 4))


def test_local_deadline_prefers_a_zone_where_it_is_2359():
    aoe = datetime(2026, 9, 20, 11, 59, tzinfo=timezone.utc)
    assert local_deadline(aoe, [None]) == (date(2026, 9, 19), "23:59", "AoE")
    et = datetime(2026, 3, 6, 4, 59, tzinfo=timezone.utc)  # 23:59 EST
    assert local_deadline(et, ["ET"]) == (date(2026, 3, 5), "23:59", "ET")
    odd = datetime(2026, 3, 5, 22, 0, tzinfo=timezone.utc)
    assert local_deadline(odd, [None]) == (date(2026, 3, 5), "22:00", "UTC")
    assert local_deadline(odd, ["CET"]) == (date(2026, 3, 5), "23:00", "CET")


def test_templates_candidates_and_title_match():
    templates = venue_templates([
        "ICLR.cc/2026/Conference", "ICLR.cc/2027/Conference",
        "thecvf.com/CVPR/2027/Conference", "ICLR.cc/2026/Workshop/Foo",
    ])
    assert templates == {"ICLR.cc/{year}/Conference": [2027, 2026],
                         "thecvf.com/CVPR/{year}/Conference": [2027]}
    row = ConferenceRow(acronym="CVPR", name="x")
    assert candidate_templates(row, templates) == ["thecvf.com/CVPR/{year}/Conference"]
    assert title_matches("Asia Pacific Bioinformatics Conference",
                         "The 23rd Asia Pacific Bioinformatics Conference")
    assert not title_matches("International Conference on Emergency Medicine",
                             "Sixteenth International Conference on Ecomaterials")


# --- Placing an edition ---------------------------------------------------------


def _edition(start=date(2027, 4, 26), abstract=(2026, 9, 19, 11, 59), paper=(2026, 9, 25, 11, 59)):
    return Edition(
        venue_id="ICLR.cc/2027/Conference", year=2027, start_date=start, location="Rio",
        abstract=datetime(*abstract, tzinfo=timezone.utc) if abstract else None,
        paper=datetime(*paper, tzinfo=timezone.utc) if paper else None,
    )


def test_plan_new_edition_fills_upcoming(tmp_path):
    plan = plan_update(_row(_db(tmp_path)), _edition(), TODAY)
    assert plan.slot == "upcoming" and not plan.new_edition
    assert plan.fields["upcoming_abstract_deadline"] == date(2026, 9, 18)
    assert plan.fields["upcoming_paper_deadline"] == date(2026, 9, 24)
    assert plan.fields["upcoming_start_date"] == date(2027, 4, 26)


def test_plan_keeps_a_deadline_within_its_grace_period(tmp_path):
    url = _db(tmp_path, upcoming_paper_deadline=date(2026, 9, 24),
              upcoming_start_date=date(2027, 4, 26))
    # 13:00 UTC is an hour after 23:59 AoE on Sep 24.
    plan = plan_update(_row(url), _edition(abstract=None, paper=(2026, 9, 25, 13, 0)), TODAY)
    assert plan.slot == "upcoming" and "upcoming_paper_deadline" not in plan.fields


def test_plan_skips_an_edition_older_than_the_stored_ones(tmp_path):
    plan = plan_update(_row(_db(tmp_path)), _edition(start=date(2025, 4, 24),
                       abstract=(2024, 9, 28, 11, 59), paper=(2024, 10, 2, 11, 59)), TODAY)
    assert plan.slot is None


def test_plan_files_an_unlabeled_deadline_by_the_series_pattern(tmp_path):
    ed = _edition(abstract=None, paper=None)
    ed.single = datetime(2026, 9, 19, 11, 59, tzinfo=timezone.utc)
    plan = plan_update(_row(_db(tmp_path)), ed, TODAY)
    assert "upcoming_abstract_deadline" in plan.fields
    assert "upcoming_paper_deadline" not in plan.fields
    # A series with only a paper deadline gets it as the paper deadline.
    (tmp_path / "p").mkdir()
    url = _db(tmp_path / "p", prior_abstract_deadline=None)
    plan = plan_update(_row(url), ed, TODAY)
    assert plan.fields["upcoming_paper_deadline"] == date(2026, 9, 18)


# --- Sync ------------------------------------------------------------------------


def test_sync_links_writes_and_covers(tmp_path):
    url = _db(tmp_path)
    client = _iclr_client()
    report = sync(url, TODAY, client=client, log=lambda m: None)
    assert report.linked == ["international-conference-on-learning-representations"]
    row = _row(url)
    assert row.openreview_venue == "ICLR.cc/{year}/Conference"
    assert (row.upcoming_abstract_deadline, row.upcoming_paper_deadline) == (
        date(2026, 9, 18), date(2026, 9, 24))
    assert row.abstract_month == 9 and row.deadline_time == "11:59 PM AoE"
    assert report.covered == {row.id: "ICLR.cc/2027/Conference"}

    # An extension made on OpenReview is recorded as one.
    client = _iclr_client(due=_ms(2026, 9, 22, 11, 59))
    sync(url, date(2026, 9, 10), client=client, log=lambda m: None)
    row = _row(url)
    assert row.upcoming_abstract_deadline == date(2026, 9, 21)
    assert row.abstract_deadline_extension == "09/18/2026 – 09/21/2026"


def test_sync_respects_pins_and_dry_run(tmp_path):
    url = _db(tmp_path, upcoming_abstract_deadline=date(2026, 9, 1),
              upcoming_start_date=date(2027, 4, 26))
    rid = "international-conference-on-learning-representations"
    sync(url, TODAY, client=_iclr_client(), dry_run=True, log=lambda m: None)
    assert _row(url).openreview_venue is None
    assert _row(url).upcoming_abstract_deadline == date(2026, 9, 1)

    set_pins(rid, ["upcoming_abstract_deadline"], db_url=url)
    sync(url, TODAY, client=_iclr_client(), log=lambda m: None)
    row = _row(url)
    assert row.upcoming_abstract_deadline == date(2026, 9, 1)  # pinned
    assert row.upcoming_paper_deadline == date(2026, 9, 24)


def test_linking_refuses_another_meeting_and_waits_on_hidden_groups(tmp_path):
    url = _db(tmp_path, acronym="ICEM", name="International Conference on Emergency Medicine",
              url="https://icem2026.org")
    other = FakeClient(groups={"ICEM/2026/Conference": _group(
        "Sixteenth International Conference on Ecomaterials", "https://icem16.ac.th")})
    sync(url, TODAY, client=other, log=lambda m: None)
    rid = "international-conference-on-emergency-medicine"
    assert _row(url, rid).openreview_venue == NO_LINK

    (tmp_path / "h").mkdir()
    url2 = _db(tmp_path / "h", acronym="KDD", name="Knowledge Discovery and Data Mining",
               url="https://kdd.org")
    hidden = FakeClient(groups={}, venues=["KDD.org/2026/Conference"])
    sync(url2, TODAY, client=hidden, log=lambda m: None)
    assert _row(url2, "knowledge-discovery-and-data-mining").openreview_venue is None


def test_set_link(tmp_path):
    url = _db(tmp_path)
    rid = "international-conference-on-learning-representations"
    assert set_link(rid, "ICLR.cc/2026/Conference", db_url=url) == "ICLR.cc/{year}/Conference"
    assert set_link(rid, "none", db_url=url) == NO_LINK
    assert set_link(rid, "auto", db_url=url) == "auto"
    assert _row(url).openreview_venue is None
    with pytest.raises(ValueError):
        set_link(rid, "ICLR.cc/Conference", db_url=url)


def test_run_watch_skips_page_check_and_research_for_covered_series(tmp_path):
    # A deadline within two weeks puts the series in the daily tier.
    url = _db(tmp_path, upcoming_abstract_deadline=date(2026, 9, 18),
              upcoming_paper_deadline=date(2026, 9, 24), upcoming_start_date=date(2027, 4, 26))
    fetched = []

    def check(u):
        fetched.append(u)
        return PageCheck(fingerprint="fp", dates=1, pages=1)

    def refresh(targets, attendance_hints=None):
        raise AssertionError("a covered series needs no research")

    report = run_watch(url, today=date(2026, 9, 10), check=check, refresh=refresh,
                       log=lambda m: None, openreview=_iclr_client())
    assert fetched == []
    [decision] = report.decisions
    assert (decision.status, decision.run_agent) == ("openreview", False)
    assert _row(url).watch_checked == date(2026, 9, 10)
