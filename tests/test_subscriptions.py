"""Offline tests for per-conference subscriber emails (``subscriptions.py``).

The subscription list and the SMTP send are injected, so these run without the
site or a mailbox. They cover the change detection, the first-run snapshot, the
retry of a failed send, and the email itself (changes, .ics attachment, signed
unsubscribe links).
"""

from datetime import date
from urllib.parse import parse_qs, urlparse

from conference_agent import subscriptions as subs
from conference_agent.models import Conference

SITE = "https://example.test"
SECRET = "s3cret"
# Ids are slugs of names.
RID = "radiological-society-of-north-america"


def _conf(**overrides):
    base = dict(
        acronym="RSNA",
        name="Radiological Society of North America",
        subcategory="radiology",
        upcoming_abstract_deadline=date(2026, 4, 8),
        upcoming_start_date=date(2026, 11, 29),
        upcoming_end_date=date(2026, 12, 3),
        url="https://www.rsna.org/annual-meeting",
    )
    base.update(overrides)
    return Conference(**base)


def _run(confs, state_path, subscribers=None, send=None, resolve=None):
    sent = []
    return subs.notify_subscribers(
        confs,
        state_path=state_path,
        site_url=SITE,
        secret=SECRET,
        from_address="bot@example.test",
        send=send or sent.append,
        fetch=lambda: subscribers or {},
        log=lambda _: None,
        resolve=resolve,
    ), sent


def test_sign_matches_the_js_implementation():
    # Value computed by web/vercel/api/_lib.js sign("unsubscribe", "a.b@x.org",
    # "RSNA") with SUBSCRIBE_SECRET=s3cret; the two must agree for links to work.
    assert subs.sign(SECRET, "unsubscribe", "a.b@x.org", "RSNA") == (
        "eb44d7e855d70556901060bc435dbda287d3e4dd0438f657a70a944a8179c1c1"
    )


def test_first_run_records_snapshot_and_sends_nothing(tmp_path):
    state = tmp_path / "state.json"
    report, sent = _run([_conf()], state, {RID: ["a@x.org"]})
    assert report.initialized and not sent
    assert subs.load_state(state)[RID]["upcoming_start_date"] == "2026-11-29"


def test_changed_series_emails_its_subscribers_only(tmp_path):
    state = tmp_path / "state.json"
    _run([_conf(), _conf(acronym="ECR", name="ECR")], state)
    moved = _conf(upcoming_start_date=date(2026, 11, 30))
    report, sent = _run(
        [moved, _conf(acronym="ECR", name="ECR")],
        state,
        {RID: ["a@x.org", "b@x.org"], "ecr": ["c@x.org"]},
    )
    assert report.changed == {RID: [("Conference start", "2026-11-29", "2026-11-30")]}
    assert sorted(m["To"] for m in sent) == ["a@x.org", "b@x.org"]
    # The snapshot advanced, so the next run is quiet.
    report, sent = _run([moved, _conf(acronym="ECR", name="ECR")], state, {RID: ["a@x.org"]})
    assert not report.changed and not sent


def test_failed_send_is_retried_next_run(tmp_path):
    state = tmp_path / "state.json"
    _run([_conf()], state)
    moved = _conf(location="Chicago")

    def boom(_msg):
        raise OSError("smtp down")

    report, _ = _run([moved], state, {RID: ["a@x.org"]}, send=boom)
    assert report.failed == [(RID, "a@x.org")]
    report, sent = _run([moved], state, {RID: ["a@x.org"]})
    assert [m["To"] for m in sent] == ["a@x.org"]


def test_new_series_is_recorded_without_email(tmp_path):
    state = tmp_path / "state.json"
    _run([_conf()], state)
    report, sent = _run([_conf(), _conf(acronym="ECR", name="ECR")], state, {"ecr": ["c@x.org"]})
    assert not report.changed and not sent
    assert "ecr" in subs.load_state(state)


def test_former_ids_resolve_to_the_current_series(tmp_path):
    """State and subscriptions stored under a former id (the acronym id before
    the move to name ids) still reach the series, and the unsubscribe link
    names the id the subscription is stored under."""
    import json

    state = tmp_path / "state.json"
    snap = subs.watched_snapshot(_conf())
    state.write_text(json.dumps({"RSNA": snap}), encoding="utf-8")
    moved = _conf(upcoming_start_date=date(2026, 11, 30))

    def resolve(ids):
        return {i: RID for i in ids if i in ("RSNA", RID)}

    report, sent = _run(
        [moved], state, {"RSNA": ["a@x.org"], "GONE": ["z@x.org"]}, resolve=resolve
    )
    assert report.changed == {RID: [("Conference start", "2026-11-29", "2026-11-30")]}
    assert [m["To"] for m in sent] == ["a@x.org"]
    q = parse_qs(urlparse(sent[0]["List-Unsubscribe"].strip("<>")).query)
    assert q["id"] == ["RSNA"]
    assert q["sig"] == [subs.sign(SECRET, "unsubscribe", "a@x.org", "RSNA")]
    assert set(subs.load_state(state)) == {RID}


def test_update_email_has_changes_ics_and_unsubscribe_links():
    conf = _conf(upcoming_paper_deadline=date(2026, 9, 1))
    msg = subs.build_update_email(
        conf, [("Paper deadline", None, "2026-09-01")], "a@x.org", "bot@example.test", SITE, SECRET
    )
    assert msg["Subject"] == "RSNA updated: paper deadline"
    body = msg.get_body(("plain",)).get_content()
    assert "- Paper deadline: 2026-09-01 (newly announced)" in body
    (attachment,) = list(msg.iter_attachments())
    assert attachment.get_filename() == f"{RID}.ics"
    assert attachment.get_content_type() == "text/calendar"
    assert "BEGIN:VCALENDAR" in attachment.get_content()

    unsub = msg["List-Unsubscribe"].strip("<>")
    q = {k: v[0] for k, v in parse_qs(urlparse(unsub).query).items()}
    assert unsub.startswith(f"{SITE}/api/unsubscribe?")
    assert q["sig"] == subs.sign(SECRET, "unsubscribe", "a@x.org", RID)
    assert subs.unsubscribe_url(SITE, SECRET, "a@x.org", "*") in body
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_rollover_email_announces_the_new_edition():
    conf = _conf(
        upcoming_abstract_deadline=None,
        upcoming_start_date=date(2027, 11, 28),
        upcoming_end_date=date(2027, 12, 2),
    )
    changes = [
        ("Abstract deadline", "2026-04-08", None),
        ("Conference start", "2026-11-29", "2027-11-28"),
        ("Conference end", "2026-12-03", "2027-12-02"),
    ]
    msg = subs.build_update_email(conf, changes, "a@x.org", "bot@example.test", SITE, SECRET)
    assert msg["Subject"] == "RSNA 2027 dates announced"
    body = msg.get_body(("plain",)).get_content()
    assert "The 2027 edition of Radiological Society of North America (RSNA)" in body
    assert "- Abstract deadline: not yet announced (previous edition: 2026-04-08)" in body
    assert "- Conference start: 2026-11-29 → 2027-11-28" in body
    assert "You will get another" in body


def test_rescheduled_dates_are_not_a_rollover():
    changes = [("Conference start", "2026-11-29", "2026-12-06")]
    assert subs.new_edition_year(changes) is None
    msg = subs.build_update_email(
        _conf(upcoming_start_date=date(2026, 12, 6)), changes, "a@x.org", "bot@example.test", SITE, SECRET
    )
    assert msg["Subject"] == "RSNA updated: conference start"


def test_moving_a_finished_edition_to_prior_sends_nothing(tmp_path):
    state = tmp_path / "state.json"
    _run([_conf()], state)
    rolled = _conf(
        upcoming_abstract_deadline=None,
        upcoming_start_date=None,
        upcoming_end_date=None,
        prior_abstract_deadline=date(2026, 4, 8),
        prior_start_date=date(2026, 11, 29),
        prior_end_date=date(2026, 12, 3),
    )
    report, sent = _run([rolled], state, {RID: ["a@x.org"]})
    assert report.changed == {} and sent == []

    # The next edition, announced later, is reported as a new edition.
    nxt = rolled.model_copy(update={"upcoming_start_date": date(2027, 11, 28)})
    report, sent = _run([nxt], state, {RID: ["a@x.org"]})
    assert len(sent) == 1
    assert sent[0]["Subject"] == "RSNA 2027 dates announced"


def _remind(confs, state_path, subscribers, today, send=None):
    sent = []
    report = subs.send_reminders(
        confs,
        state_path=state_path,
        site_url=SITE,
        secret=SECRET,
        from_address="bot@example.test",
        send=send or sent.append,
        subscribers=lambda: {cid: {e: cid for e in emails} for cid, emails in subscribers.items()},
        lead_days=7,
        today=today,
        log=lambda _: None,
    )
    return report, sent


def test_reminder_is_sent_once_a_week_before_a_deadline(tmp_path):
    state = tmp_path / "reminders.json"
    conf = _conf(abstract_time="23:59", abstract_timezone="AoE")
    # Eight days out: too early.
    _, sent = _remind([conf], state, {RID: ["a@x.org"]}, date(2026, 3, 31))
    assert sent == []
    _, sent = _remind([conf], state, {RID: ["a@x.org"]}, date(2026, 4, 1))
    assert len(sent) == 1
    msg = sent[0]
    assert msg["Subject"] == "Reminder: RSNA abstract deadline in 7 days (Apr 8)"
    text = msg.get_content()
    assert "Wednesday, April 8, 2026" in text
    assert "Deadline time: 11:59 PM AoE" in text
    assert "api/unsubscribe?" in text and msg["List-Unsubscribe"]
    # Not again the next day; a new subscriber still gets one.
    _, sent = _remind([conf], state, {RID: ["a@x.org", "b@x.org"]}, date(2026, 4, 2))
    assert [m["To"] for m in sent] == ["b@x.org"]


def test_failed_reminder_is_retried_and_past_entries_pruned(tmp_path):
    import json

    state = tmp_path / "reminders.json"
    conf = _conf()

    def boom(msg):
        raise OSError("smtp down")

    report, _ = _remind([conf], state, {RID: ["a@x.org"]}, date(2026, 4, 5), send=boom)
    assert report.failed == [(RID, "abstract", "a@x.org")]
    _, sent = _remind([conf], state, {RID: ["a@x.org"]}, date(2026, 4, 6))
    assert len(sent) == 1 and sent[0]["Subject"].endswith("in 2 days (Apr 8)")
    assert json.loads(state.read_text())
    _remind([conf], state, {RID: ["a@x.org"]}, date(2026, 4, 9))
    assert json.loads(state.read_text()) == {}


def test_no_subscription_fetch_when_no_reminder_is_due(tmp_path):
    def fetch():
        raise AssertionError("fetched without a due reminder")

    report = subs.send_reminders(
        [_conf()], tmp_path / "r.json", SITE, SECRET, "bot@example.test",
        send=lambda m: None, subscribers=fetch, lead_days=7, today=date(2026, 1, 1),
        log=lambda _: None,
    )
    assert report.due == []
