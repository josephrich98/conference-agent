"""Constants, controlled vocabularies, and seed lists.

Centralizes values that are referenced across modules so they are changed in one
place: the Anthropic model id used by the discovery agent, default database
location, and a seed list of conferences to bootstrap discovery.
"""

from __future__ import annotations

import os

from conference_agent.models import ConferenceTier

# --- Discovery agent -------------------------------------------------------

# Anthropic model id used by the discovery agent. Centralized here so it is
# updated in one place. Consult the `claude-api` skill for the current id.
ANTHROPIC_MODEL = os.environ.get("CONFERENCE_AGENT_MODEL", "claude-opus-4-8")

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# --- Storage ---------------------------------------------------------------

# Default SQLAlchemy URL. Overridable via env or the `--db` CLI flag. The file
# lives under data/, which is gitignored.
DEFAULT_DATABASE_URL = os.environ.get(
    "CONFERENCE_DATABASE_URL", "sqlite:///data/conferences.db"
)

# --- Google Calendar -------------------------------------------------------

GOOGLE_OAUTH_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
# Target calendar; "primary" is the user's default calendar.
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# --- Seed list -------------------------------------------------------------

# A small seed of well-known conferences to bootstrap and sanity-check the
# discovery agent. Tiers are illustrative starting points, not authoritative.
# (acronym, full name, topic, tier)
SEED_CONFERENCES = [
    ("RSNA", "Radiological Society of North America Annual Meeting", "Radiology", ConferenceTier.BIG),
    ("SPR", "Society for Pediatric Radiology Annual Meeting", "Radiology", ConferenceTier.MEDIUM),
    ("ARRS", "American Roentgen Ray Society Annual Meeting", "Radiology", ConferenceTier.MEDIUM),
    ("SIIM", "Society for Imaging Informatics in Medicine Annual Meeting", "Imaging Informatics", ConferenceTier.MEDIUM),
]
