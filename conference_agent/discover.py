"""Conference discovery agent.

Drives the Anthropic API (tool use / web search) to find conferences and extract
their structured fields as `Conference` records. This is a stub: the agent loop,
tool definitions, and structured-output handling are not yet implemented.

When implementing, consult the `claude-api` skill for current model ids,
web-search/tool-use patterns, and structured-output guidance. The model id is
configured in `conference_agent.config.ANTHROPIC_MODEL`.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from conference_agent.config import ANTHROPIC_MODEL
from conference_agent.models import Conference


def discover_conferences(
    topics: Optional[Iterable[str]] = None,
    year: Optional[int] = None,
    model: str = ANTHROPIC_MODEL,
) -> List[Conference]:
    """Discover conferences for the given topics/year and return typed records.

    Args:
        topics: Domains to search (e.g. ["Radiology"]). Defaults to the seed set.
        year: Edition year to target. Defaults to the upcoming cycle.
        model: Anthropic model id to drive the agent.

    Returns:
        A list of `Conference` records.

    Not yet implemented.
    """
    raise NotImplementedError("Discovery agent is not yet implemented.")
