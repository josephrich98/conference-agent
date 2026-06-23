"""AWS Lambda entry point for the scheduled conference refresh.

Invoked by EventBridge Scheduler (see ``infra/template.yaml``), this replaces the
retired GitHub Actions refresh workflows. Each schedule passes a ``cadence`` in
its input to pick which fields to re-discover, mirroring the old weekly/monthly
jobs:

    {"cadence": "weekly"}    flagship fields  (config.weekly_subcategories)
    {"cadence": "monthly"}   the rest         (config.monthly_subcategories)
    {"cadence": "due"}       only series due for an auto-check (refresh.due_*)
    {"cadence": "all"}       every seeded field

It runs the same discovery + idempotent upsert as ``scripts/daily_update.py`` but
shaped as a Lambda handler. It uses the ``api`` discovery backend, so it needs
``ANTHROPIC_API_KEY`` and outbound internet (the SAM template gives the function
a NAT route) and writes to the shared RDS database via ``CONFERENCE_DATABASE_URL``.
Email notification is intentionally omitted (no SMTP in this deployment).

Referenced by the SAM template as ``Handler: web.refresh_handler.handler``.
"""

from __future__ import annotations

import os

from conference_agent.config import (
    DEFAULT_DATABASE_URL,
    STANDING_SUBCATEGORIES,
    monthly_subcategories,
    weekly_subcategories,
)
from conference_agent.database import known_attendance_sources, upsert_conferences
from conference_agent.discover import discover_conferences
from conference_agent.refresh import due_subcategories, mark_subcategories_checked

# Static cadence -> subcategory selectors. "due" is data-dependent and resolved
# separately below, matching scripts/daily_update.py.
_CADENCE_SUBCATEGORIES = {
    "weekly": weekly_subcategories,
    "monthly": monthly_subcategories,
    "all": lambda: list(STANDING_SUBCATEGORIES),
}


def _resolve_subcategories(cadence: str, db_url: str) -> list[str]:
    if cadence == "due":
        return due_subcategories(db_url)
    selector = _CADENCE_SUBCATEGORIES.get(cadence)
    if selector is None:
        raise ValueError(
            f"unknown cadence {cadence!r}; expected one of "
            f"{sorted([*_CADENCE_SUBCATEGORIES, 'due'])}"
        )
    return selector()


def handler(event, context):  # noqa: ANN001 - Lambda event/context are untyped
    """Run a scheduled refresh. ``event['cadence']`` selects the fields."""
    cadence = (event or {}).get("cadence", "weekly")
    db_url = os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL)

    subcategories = _resolve_subcategories(cadence, db_url)
    if not subcategories:
        return {"cadence": cadence, "subcategories": [], "upserted": 0}

    total = 0
    for subcategory in subcategories:
        hints = known_attendance_sources(db_url=db_url, subcategories=[subcategory])
        conferences = discover_conferences(
            subcategories=[subcategory], backend="api", attendance_hints=hints
        )
        total += upsert_conferences(conferences, db_url=db_url)

    # Mirror daily_update: stamp the refreshed fields so the auto-check interval
    # gates the next "due" run.
    if cadence == "due":
        mark_subcategories_checked(subcategories, db_url=db_url)

    return {"cadence": cadence, "subcategories": subcategories, "upserted": total}
