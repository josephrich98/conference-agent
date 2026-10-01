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


def _run(confs, state_path, subscribers=None, send=None):
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
    ), sent


def test_sign_matches_the_js_implementation():
    # Value computed by web/vercel/api/_lib.js sign("unsubscribe", "a.b@x.org",
    # "RSNA") with SUBSCRIBE_SECRET=s3cret; the two must agree for links to work.
    assert subs.sign(SECRET, "unsubscribe", "a.b@x.org", "RSNA") == (
        "eb44d7e855d70556901060bc435dbda287d3e4dd0438f657a70a944a8179c1c1"
    )


def test_first_run_records_snapshot_and_sends_nothing(tmp_path):
    state = tmp_path / "state.json"
    report, sent = _run([_conf()], state, {"RSNA": ["a@x.org"]})
    assert report.initialized and not sent
    assert subs.load_state(state)["RSNA"]["upcoming_start_date"] == "2026-11-29"


def test_changed_series_emails_its_subscribers_only(tmp_path):
    state = tmp_path / "state.json"
    _run([_conf(), _conf(acronym="ECR", name="ECR")], state)
    moved = _conf(upcoming_start_date=date(2026, 11, 30))
    report, sent = _run(
        [moved, _conf(acronym="ECR", name="ECR")],
        state,
        {"RSNA": ["a@x.org", "b@x.org"], "ECR": ["c@x.org"]},
    )
    assert report.changed == {"RSNA": [("Conference start", "2026-11-29", "2026-11-30")]}
    assert sorted(m["To"] for m in sent) == ["a@x.org", "b@x.org"]
    # The snapshot advanced, so the next run is quiet.
    report, sent = _run([moved, _conf(acronym="ECR", name="ECR")], state, {"RSNA": ["a@x.org"]})
    assert not report.changed and not sent


def test_failed_send_is_retried_next_run(tmp_path):
    state = tmp_path / "state.json"
    _run([_conf()], state)
    moved = _conf(location="Chicago")

    def boom(_msg):
        raise OSError("smtp down")

    report, _ = _run([moved], state, {"RSNA": ["a@x.org"]}, send=boom)
    assert report.failed == [("RSNA", "a@x.org")]
    report, sent = _run([moved], state, {"RSNA": ["a@x.org"]})
    assert [m["To"] for m in sent] == ["a@x.org"]


def test_new_series_is_recorded_without_email(tmp_path):
    state = tmp_path / "state.json"
    _run([_conf()], state)
    report, sent = _run([_conf(), _conf(acronym="ECR", name="ECR")], state, {"ECR": ["c@x.org"]})
    assert not report.changed and not sent
    assert "ECR" in subs.load_state(state)


def test_update_email_has_changes_ics_and_unsubscribe_links():
    conf = _conf(upcoming_paper_deadline=date(2026, 9, 1))
    msg = subs.build_update_email(
        conf, [("Paper deadline", None, "2026-09-01")], "a@x.org", "bot@example.test", SITE, SECRET
    )
    assert msg["Subject"] == "RSNA updated: paper deadline"
    body = msg.get_body(("plain",)).get_content()
    assert "- Paper deadline: 2026-09-01 (newly announced)" in body
    (attachment,) = list(msg.iter_attachments())
    assert attachment.get_filename() == "RSNA.ics"
    assert attachment.get_content_type() == "text/calendar"
    assert "BEGIN:VCALENDAR" in attachment.get_content()

    unsub = msg["List-Unsubscribe"].strip("<>")
    q = {k: v[0] for k, v in parse_qs(urlparse(unsub).query).items()}
    assert unsub.startswith(f"{SITE}/api/unsubscribe?")
    assert q["sig"] == subs.sign(SECRET, "unsubscribe", "a@x.org", "RSNA")
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
