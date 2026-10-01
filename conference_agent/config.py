"""Constants and controlled vocabularies.

Centralizes values that are referenced across modules so they are changed in one
place: the Anthropic model id used by the discovery agent, default database
location, notification settings, and the refresh schedule.
"""

from __future__ import annotations

import os

# --- Discovery agent -------------------------------------------------------

# Anthropic model id used by the discovery agent. Centralized here so it is
# updated in one place. Consult the `claude-api` skill for the current id.
ANTHROPIC_MODEL = os.environ.get("CONFERENCE_AGENT_MODEL", "claude-opus-4-8")

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# --- Natural-language search (local LLM) -----------------------------------

# Optional natural-language → boolean-query translation for the web table runs
# against a free, local Ollama server (https://ollama.com), so it needs no API
# key and no network egress. The base URL points at Ollama's HTTP API; the model
# is a small, fast instruction model (e.g. ``llama3.2:3b`` or ``qwen2.5:1.5b``).
# Both are overridable via the environment. The feature degrades gracefully: when
# the server is unreachable the web UI still works with the manual boolean box.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
NL_QUERY_MODEL = os.environ.get("CONFERENCE_NL_QUERY_MODEL", "qwen2.5:1.5b")
# Seconds to wait on the local model before giving up (small models are fast, but
# a cold load can take a few seconds).
NL_QUERY_TIMEOUT = float(os.environ.get("CONFERENCE_NL_QUERY_TIMEOUT", "30"))

# --- Storage ---------------------------------------------------------------

# Default SQLAlchemy URL. Overridable via env or the `--db` CLI flag. The file
# lives under data/, which is gitignored.
DEFAULT_DATABASE_URL = os.environ.get(
    "CONFERENCE_DATABASE_URL", "sqlite:///data/conferences.db"
)

# --- Notifications ---------------------------------------------------------

# Where to send the "table refreshed" email after a discovery / daily run.
# Defaults to the project owner; override via env. SMTP settings are read from
# the environment so no credentials are committed (see calendar_sync/notify).
NOTIFY_EMAIL = os.environ.get("CONFERENCE_NOTIFY_EMAIL", "josephrich98@gmail.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")  # e.g. a Gmail address
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")  # e.g. a Gmail app password

# Per-conference update emails for site visitors (see subscriptions.py). The
# secret signs unsubscribe links and authorizes reading the subscription list;
# it must equal SUBSCRIBE_SECRET in the Vercel project's environment.
SUBSCRIBE_SECRET = os.environ.get("SUBSCRIBE_SECRET")
SITE_URL = os.environ.get("CONFERENCE_SITE_URL", "https://conferenceagent.vercel.app")

# --- Calendar feed (.ics) --------------------------------------------------

# Lead times (in days before the event) for the reminders attached to every
# event in the iCalendar feed -- four weeks, one week, and one day ahead.
CALENDAR_REMINDER_LEAD_DAYS = (28, 7, 1)
# Hour of day (local, 24h) the reminders fire on their target morning. An all-day
# event starts at midnight, so anchoring the alarm at this hour keeps calendar
# apps from labeling a midnight alarm as one day too early (see
# ``calendar_sync._alarm_trigger``).
CALENDAR_REMINDER_HOUR = 9

# Days before an upcoming submission deadline that subscribers of the series get
# one reminder email (``subscriptions.send_reminders``).
SUBSCRIBER_REMINDER_DAYS = 7

# --- Refresh cadence -------------------------------------------------------

# Discovery runs per field (one web-search pass per subcategory), so cadence is
# set per field, not per conference. Fields that carry flagship, fast-moving
# meetings are refreshed weekly; every other field in the table refreshes
# monthly. Editing this set is the single knob for a field's cadence.
WEEKLY_SUBCATEGORIES = {
    "radiology",
    "cardiology",
    "oncology",
    "genomics",
    "machine learning",
}


def weekly_subcategories(fields: "list[str]") -> list[str]:
    """The given fields refreshed weekly (those in WEEKLY_SUBCATEGORIES)."""
    return sorted(s for s in fields if s in WEEKLY_SUBCATEGORIES)


def monthly_subcategories(fields: "list[str]") -> list[str]:
    """The given fields refreshed monthly (everything not refreshed weekly)."""
    return sorted(s for s in fields if s not in WEEKLY_SUBCATEGORIES)


# --- Per-conference auto-check policy --------------------------------------
#
# A finer schedule layered on top of the per-field cadence above, keyed on each
# series' most recent known edition. The intent is to spend discovery calls only
# when a new edition is plausibly about to be announced.
#
# A series becomes "due for a check" once its latest known edition is between
# CHECK_WINDOW_MIN_MONTHS and CHECK_WINDOW_MAX_MONTHS old -- measured from that
# edition's earliest submission deadline, or its start date when no deadline is
# known: old enough that the next edition's dates may be published soon, but
# recent enough to assume the series is still active (the two-year ceiling keeps
# biennial meetings in view). While inside that window it is re-checked every
# RECHECK_INTERVAL_DAYS days until either a future ("upcoming") edition is found
# -- at which point it is updated and no longer due -- or the edition ages past
# the maximum, at which point checking stops. See ``conference_agent.refresh``.
CHECK_WINDOW_MIN_MONTHS = 6
CHECK_WINDOW_MAX_MONTHS = 24
RECHECK_INTERVAL_DAYS = 14

# --- Page-watch schedule (``daily_update.py --cadence watch``) ---------------
#
# The watch cadence runs daily but spends a discovery (agent) call on a series
# only when a cheap, agent-free check of its official pages suggests something
# changed. Each series falls into at most one tier (see ``refresh.watch_tier``):
#
# - "daily": an upcoming submission deadline lies within WATCH_DAILY_WINDOW_DAYS
#   before or after today -- when extensions are announced, both just before and
#   shortly after a deadline. Its pages are checked every run.
# - "soon": an upcoming deadline or the meeting's start date lies within the next
#   WATCH_SOON_WINDOW_DAYS days. Checked every RECHECK_INTERVAL_DAYS days.
# - "stale": no future edition on record and the latest edition is inside the
#   CHECK_WINDOW_* window above. Checked every RECHECK_INTERVAL_DAYS days.
#
# The cheap check (``conference_agent.page_watch``) fetches the official link and
# a few same-site "dates" / "deadlines" / "call for abstracts" pages and hashes
# the set of dates they mention. The agent runs when that fingerprint changes;
# when the pages cannot be read it falls back to one agent run per
# RECHECK_INTERVAL_DAYS; and even an unchanged page gets an agent run after
# WATCH_BACKSTOP_DAYS, since a new edition is often announced on a new site
# (e.g. a year-stamped domain) that the stored link never shows.
WATCH_DAILY_WINDOW_DAYS = 14
WATCH_SOON_WINDOW_DAYS = 30
WATCH_BACKSTOP_DAYS = 28
# Conferences re-researched per agent session, and the most re-researched in one
# run. Anything over the cap is deferred (left unstamped) to the next run, so a
# burst of changes spreads over several days instead of one long run.
WATCH_BATCH_SIZE = 6
WATCH_MAX_AGENT_PER_RUN = 18
