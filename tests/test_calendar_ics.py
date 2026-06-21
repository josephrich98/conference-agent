"""Offline unit tests for the iCalendar (``.ics``) feed builder.

These exercise :func:`conferences_to_ics` and its helpers, which are pure Python
(no network), so CI stays hermetic. They lock in the feed a user subscribes to:
one all-day event per populated deadline / date range, an exclusive all-day end,
reminders four weeks / one week / one day ahead, stable UIDs (idempotent
re-fetch), and RFC 5545 escaping, folding, and CRLF.
"""

from datetime import date, datetime, timezone

from conference_agent import calendar_sync as cs
from conference_agent.config import CALENDAR_REMINDER_LEAD_DAYS
from conference_agent.models import Conference

# Fixed stamp so output is deterministic.
STAMP = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)


def _conf(**overrides):
    base = dict(
        acronym="RSNA",
        name="Radiological Society of North America",
        subcategory="radiology",
        upcoming_abstract_deadline=date(2026, 4, 8),
        upcoming_paper_deadline=date(2026, 9, 1),
        upcoming_start_date=date(2026, 11, 29),
        upcoming_end_date=date(2026, 12, 3),
        url="https://www.rsna.org/annual-meeting",
    )
    base.update(overrides)
    return Conference(**base)


def _ics(conferences, **kwargs):
    kwargs.setdefault("dtstamp", STAMP)
    return cs.conferences_to_ics(conferences, **kwargs)


def test_calendar_is_wrapped_and_crlf_terminated():
    ics = _ics([_conf()])
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.rstrip("\r\n").endswith("END:VCALENDAR")
    # Every line ends with CRLF (RFC 5545 §3.1).
    assert "\n" in ics
    assert all(line.endswith("\r") for line in ics.split("\n") if line)
    assert "VERSION:2.0\r\n" in ics
    assert "PRODID:" in ics


def test_full_edition_yields_three_events():
    ics = _ics([_conf()])
    assert ics.count("BEGIN:VEVENT") == 3
    assert ics.count("END:VEVENT") == 3
    assert "SUMMARY:RSNA — abstract deadline" in ics
    assert "SUMMARY:RSNA — paper deadline" in ics
    assert "SUMMARY:RSNA 2026" in ics


def test_registration_text_yields_no_event():
    # Registration is free text (windows, not a date), so it produces no event:
    # the feed still has only the three deadline/date events.
    ics = _ics([_conf(upcoming_registration="Early bird: Jan 5 - Mar 1")])
    assert ics.count("BEGIN:VEVENT") == 3
    assert "registration" not in ics.lower()


def test_conference_dates_use_exclusive_all_day_end():
    ics = _ics([_conf()])
    assert "DTSTART;VALUE=DATE:20261129" in ics
    # Last day Dec 3 + 1, since an all-day DTEND is exclusive.
    assert "DTEND;VALUE=DATE:20261204" in ics


def test_single_day_deadline_spans_one_day():
    ics = _ics([_conf(upcoming_paper_deadline=None, upcoming_start_date=None,
                      upcoming_end_date=None)])
    assert ics.count("BEGIN:VEVENT") == 1
    assert "DTSTART;VALUE=DATE:20260408" in ics
    assert "DTEND;VALUE=DATE:20260409" in ics


def test_missing_fields_produce_no_events():
    conf = _conf(
        upcoming_abstract_deadline=None,
        upcoming_paper_deadline=None,
        upcoming_start_date=None,
        upcoming_end_date=None,
    )
    ics = _ics([conf])
    assert "BEGIN:VEVENT" not in ics


def test_every_event_has_day_week_month_alarms():
    ics = _ics([_conf()])
    # Three events × three lead times.
    assert ics.count("BEGIN:VALARM") == 9
    # Each alarm is anchored to the morning N days before (not bare midnight), so
    # calendar apps label "N days before" correctly.
    for days in CALENDAR_REMINDER_LEAD_DAYS:
        assert f"TRIGGER:{cs._alarm_trigger(days)}" in ics


def test_alarm_triggers_are_anchored_to_the_morning():
    # 9 AM the day before / week before / four weeks before an all-day (midnight)
    # event: 24*N - 9 hours before the start.
    assert cs._alarm_trigger(1) == "-PT15H"
    assert cs._alarm_trigger(7) == "-P6DT15H"
    assert cs._alarm_trigger(28) == "-P27DT15H"


def test_uids_are_stable_distinct_and_namespaced():
    first = _ics([_conf()])
    second = _ics([_conf()])
    assert first == second  # deterministic given a fixed dtstamp
    uids = [ln[4:] for ln in first.split("\r\n") if ln.startswith("UID:")]
    assert len(uids) == 3
    assert len(set(uids)) == 3
    assert all(u.endswith("@conference-agent") for u in uids)


def test_text_values_are_escaped():
    conf = _conf(name="Foo, Bar; Baz")
    ics = _ics([conf])
    # Comma and semicolon in a TEXT value are backslash-escaped.
    assert "Foo\\, Bar\\; Baz" in ics


def test_long_lines_are_folded_to_75_octets():
    long_name = "A" * 200
    ics = _ics([_conf(name=long_name)])
    for line in ics.split("\r\n"):
        # A folded continuation line starts with a space; the octet length of any
        # single unfolded physical line stays within the RFC limit.
        assert len(line.encode("utf-8")) <= 75


def test_url_property_emitted_when_present():
    assert "URL:https://www.rsna.org/annual-meeting" in _ics([_conf()])
    assert "URL:" not in _ics([_conf(url=None)])
