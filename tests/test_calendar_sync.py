"""Offline unit tests for Google Calendar event construction.

These exercise only the event-building helpers (``_events_for`` / ``_reminders``),
which are pure and import no Google libraries, so CI stays hermetic (no network,
no OAuth). They lock in what the per-row / "Sync all" buttons push to Calendar:
the conference dates, both submission deadlines, and a reminder at one day, one
week, and one month before each.
"""

from datetime import date

from conference_agent import calendar_sync as cs
from conference_agent.config import (
    CALENDAR_REMINDER_LEAD_DAYS,
    GOOGLE_MAX_REMINDER_MINUTES,
)
from conference_agent.models import Conference


def _conf(**overrides):
    base = dict(
        acronym="RSNA",
        name="Radiological Society of North America",
        category="radiology",
        upcoming_abstract_deadline=date(2026, 4, 8),
        upcoming_paper_deadline=date(2026, 9, 1),
        upcoming_start_date=date(2026, 11, 29),
        upcoming_end_date=date(2026, 12, 3),
        url="https://www.rsna.org/annual-meeting",
    )
    base.update(overrides)
    return Conference(**base)


def _summaries(events):
    return [e["summary"] for e in events]


def test_lead_days_are_one_day_week_month():
    """The configured lead times are one day, one week, and one month."""
    assert set(CALENDAR_REMINDER_LEAD_DAYS) == {1, 7, 30}


def test_sync_builds_deadlines_and_conference_dates():
    """A fully-populated edition yields abstract, paper, and conference events."""
    events = cs._events_for(_conf())

    assert len(events) == 3
    summaries = _summaries(events)
    assert any("abstract deadline" in s for s in summaries)
    assert any("paper deadline" in s for s in summaries)
    assert any(s == "RSNA 2026" for s in summaries)


def test_conference_event_spans_start_to_end_exclusive():
    """The conference event covers start..end (Google's end date is exclusive)."""
    events = cs._events_for(_conf())
    conference = next(e for e in events if e["summary"] == "RSNA 2026")

    assert conference["start"] == {"date": "2026-11-29"}
    # End is the last day (Dec 3) + 1, since Google treats the end as exclusive.
    assert conference["end"] == {"date": "2026-12-04"}


def test_every_event_carries_day_week_month_reminders():
    """Each synced event reminds at one day, one week, and one month before."""
    day = 1 * 24 * 60
    week = 7 * 24 * 60
    # One month (30 days) exceeds Google's 4-week override cap, so it is clamped.
    month = min(30 * 24 * 60, GOOGLE_MAX_REMINDER_MINUTES)

    for event in cs._events_for(_conf()):
        reminders = event["reminders"]
        assert reminders["useDefault"] is False
        minutes = {o["minutes"] for o in reminders["overrides"]}
        assert all(o["method"] == "popup" for o in reminders["overrides"])
        assert minutes == {day, week, month}


def test_missing_fields_are_skipped():
    """Only the populated upcoming fields produce events."""
    events = cs._events_for(
        _conf(
            upcoming_abstract_deadline=None,
            upcoming_paper_deadline=None,
        )
    )
    assert _summaries(events) == ["RSNA 2026"]


def test_event_ids_are_stable_and_distinct():
    """Each kind gets a deterministic, distinct id (idempotent re-sync)."""
    first = {e["id"] for e in cs._events_for(_conf())}
    second = {e["id"] for e in cs._events_for(_conf())}
    assert first == second
    assert len(first) == 3
