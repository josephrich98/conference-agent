"""Conference discovery agent.

Finds major conferences in a category via web search, then extracts the results
into typed ``Conference`` records using structured outputs. The flow is always
two-phase, which keeps each step simple and robust:

1. **Research** — an agentic loop with a web-search tool. The model searches the
   web for the leading conferences in each category and writes up what it found
   (names, dates, deadlines, cost, links).
2. **Extract** — a single tool-less call that turns that research text into a
   validated list of ``Conference`` objects via a JSON schema derived from the
   pydantic model.

Two interchangeable backends run that flow (chosen by ``backend``):

- ``"claude-code"`` (default) shells out to the local ``claude`` CLI in headless
  mode (``claude -p``). It authenticates through the user's Claude Code
  subscription -- the subprocess is run with ``ANTHROPIC_API_KEY`` removed from
  its environment so the CLI never falls back to the metered API -- which makes
  it the cheaper option when a subscription is available.
- ``"api"`` calls the Anthropic API directly via the ``anthropic`` SDK. It
  requires ``ANTHROPIC_API_KEY`` (and API credits) and is opt-in.

The model id for the API backend is centralized in
``conference_agent.config.ANTHROPIC_MODEL``; the CLI backend defaults to whatever
model Claude Code is configured to use unless ``model`` is given.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date
from typing import Iterable, List, Optional

from pydantic import BaseModel

from conference_agent.config import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_MODEL,
    SEED_CONFERENCES,
    normalize_reputation,
)
from conference_agent.models import Conference, ConferenceTier, RemoteOption

# Available discovery backends. ``claude-code`` is the default (cheaper when a
# Claude Code subscription is available); ``api`` uses the metered Anthropic API.
DISCOVERY_BACKENDS = ("claude-code", "api")
DEFAULT_BACKEND = "claude-code"

# Timeouts (seconds) for the headless ``claude`` CLI calls. Research runs a
# web-search loop and can take a while; extraction is a single tool-less call.
_CLI_RESEARCH_TIMEOUT = 600
_CLI_EXTRACT_TIMEOUT = 180

# Server-side web search tool (current version supports dynamic filtering).
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

# Cap the server-side tool loop so a runaway search can't spin forever.
_MAX_CONTINUATIONS = 8

_RESEARCH_SYSTEM = """\
You are a research assistant that compiles a table of the major academic and \
professional conferences in a given field. Use web search to find authoritative, \
up-to-date information from official conference and society websites.

For each notable conference in the requested category, gather:
- acronym and full name
- the field/category it belongs to
- the most recent (prior) edition: abstract submission deadline, full paper /
  manuscript deadline, and the conference start and end dates
- the upcoming edition: abstract submission deadline, full paper / manuscript
  deadline, and the conference start and end dates
- the host city / venue (location) of each edition
- the official website URL
- whether it can be attended remotely (in-person, virtual, or hybrid)
- the registration cost: give the actual dollar figure(s) when available (e.g.
  "$1,095 member / $1,395 non-member, early-bird"), not just "varies"
- a reputability tier: big, medium, or small, relative to the field

Abstract and paper deadlines matter as much as the meeting dates: check each \
conference's "Call for Abstracts", "Submit", or "Important Dates" page and report \
the abstract submission deadline and the full paper / manuscript deadline for \
both the prior and the upcoming edition. Many series publish these on a separate \
page from the meeting dates, so search for them explicitly. State each deadline \
date you find; only say a deadline is unannounced after looking for it.

Two cases recur and are easy to get wrong:
- A submission deadline that has already passed still belongs to the edition it \
  opened. If the meeting itself is still upcoming, its (already-closed) abstract \
  deadline is the UPCOMING edition's abstract deadline, not a prior-edition date.
- Most clinical / medical-society meetings (e.g. RSNA, ASCO, ACC) accept \
  ABSTRACTS only and have no separate full-paper / manuscript deadline. Record a \
  paper deadline only when the conference genuinely has a distinct manuscript or \
  full-paper submission deadline (common for CS / ML proceedings venues such as \
  NeurIPS or CVPR). Never copy the abstract deadline into the paper field, and \
  leave the paper deadline unstated when none exists.

Prefer official sources. If a date or fact is not announced yet, say so rather \
than guessing. Today's date matters: an edition whose dates have passed is \
"prior"; the next scheduled edition is "upcoming". When the upcoming edition is \
not yet announced, the prior edition's dates are still useful as a reference.

Cover the well-known conferences thoroughly, and be sure to include every \
conference in this list (search for each by name if needed):
{seed_list}

Write up what you find clearly, one conference at a time."""

_EXTRACT_SYSTEM = """\
Convert the research notes into structured conference records. Use ISO dates \
(YYYY-MM-DD) for date fields. Use an empty string "" for any field the notes do \
not state; do not invent values. Capture every date the notes give: populate the \
abstract submission deadline and the full paper / manuscript deadline for both \
the prior and upcoming editions whenever the notes mention them, keeping each \
deadline with the correct edition. A submission deadline that has already passed \
still belongs to the edition it opened; if the meeting itself is upcoming, keep \
its abstract deadline in the upcoming column. Set the paper deadline only when \
the notes give a distinct full-paper / manuscript deadline; leave it "" for \
abstract-only meetings rather than repeating the abstract deadline. The location \
field is the host city / venue \
(e.g. "Chicago, IL" or "Vienna, Austria"). The cost field should carry the \
actual price figure(s) when the notes give one (e.g. "$1,095 member, \
early-bird"). The reputation field must be "big", "medium", "small", or "". The \
remote_option field must be "in-person", "virtual", "hybrid", "unknown", or ""."""


class _ExtractedConference(BaseModel):
    """Flat, all-string extraction target.

    Structured outputs cap schema complexity, so the model returns plain strings
    (empty when unknown) rather than the richer ``Conference`` schema with its
    optional date/enum fields. We parse and validate into ``Conference`` below.
    """

    acronym: str
    name: str
    category: str
    prior_abstract_deadline: str
    prior_paper_deadline: str
    prior_start_date: str
    prior_end_date: str
    upcoming_abstract_deadline: str
    upcoming_paper_deadline: str
    upcoming_start_date: str
    upcoming_end_date: str
    location: str
    url: str
    remote_option: str
    cost: str
    reputation: str
    notes: str


class _ExtractedList(BaseModel):
    """Wrapper so the model returns a list under a single JSON schema."""

    conferences: List[_ExtractedConference]


def _parse_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


def _clean(value: str) -> Optional[str]:
    value = value.strip() if value else ""
    return value or None


def _to_conference(item: _ExtractedConference) -> Optional[Conference]:
    """Convert a flat extracted record into a typed ``Conference`` (or None)."""
    if not (item.acronym.strip() and item.name.strip() and item.category.strip()):
        return None

    try:
        reputation = ConferenceTier(item.reputation.strip().lower()) if item.reputation.strip() else None
    except ValueError:
        reputation = None
    try:
        remote = RemoteOption(item.remote_option.strip().lower()) if item.remote_option.strip() else None
    except ValueError:
        remote = None

    # Apply the house reputation policy: only flagship conferences are "big".
    reputation = normalize_reputation(item.acronym, reputation)

    return Conference(
        acronym=item.acronym.strip(),
        name=item.name.strip(),
        category=item.category.strip().lower(),
        prior_abstract_deadline=_parse_date(item.prior_abstract_deadline),
        prior_paper_deadline=_parse_date(item.prior_paper_deadline),
        prior_start_date=_parse_date(item.prior_start_date),
        prior_end_date=_parse_date(item.prior_end_date),
        upcoming_abstract_deadline=_parse_date(item.upcoming_abstract_deadline),
        upcoming_paper_deadline=_parse_date(item.upcoming_paper_deadline),
        upcoming_start_date=_parse_date(item.upcoming_start_date),
        upcoming_end_date=_parse_date(item.upcoming_end_date),
        location=_clean(item.location),
        url=_clean(item.url),
        remote_option=remote,
        cost=_clean(item.cost),
        reputation=reputation,
        notes=_clean(item.notes),
    )


def _seed_checklist(categories: List[str]) -> str:
    """Bullet list of seed conferences in the requested categories (or all)."""
    cats = {c.strip().lower() for c in categories}
    lines = [
        f"- {acronym} — {name}"
        for acronym, name, category, _ in SEED_CONFERENCES
        if not cats or category.lower() in cats
    ]
    return "\n".join(lines) if lines else "- (no seeds for this category)"


def _research_prompt(categories: List[str]) -> str:
    """The user-facing research instruction, shared by both backends."""
    return (
        "Find the major conferences in the following "
        f"{'categories' if len(categories) > 1 else 'category'}: "
        f"{', '.join(categories)}. Search the web for current dates and details, "
        "then summarize each conference and its key dates."
    )


def _research(client, categories: List[str], model: str, max_tokens: int) -> str:
    """Run the web-search agentic loop and return the model's research text."""
    system = _RESEARCH_SYSTEM.format(seed_list=_seed_checklist(categories))
    prompt = _research_prompt(categories)
    messages = [{"role": "user", "content": prompt}]

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        tools=[_WEB_SEARCH_TOOL],
        messages=messages,
    )

    # The web-search tool runs a server-side loop; on pause_turn we re-send the
    # accumulated turn so the server resumes where it left off.
    continuations = 0
    while response.stop_reason == "pause_turn" and continuations < _MAX_CONTINUATIONS:
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response.content},
        ]
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            tools=[_WEB_SEARCH_TOOL],
            messages=messages,
        )
        continuations += 1

    return "\n".join(block.text for block in response.content if block.type == "text")


def _extract(client, research_text: str, model: str, max_tokens: int) -> List[Conference]:
    """Turn research text into validated ``Conference`` records."""
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": research_text}],
        output_format=_ExtractedList,
    )
    parsed = response.parsed_output
    if not parsed:
        return []
    return [c for c in (_to_conference(item) for item in parsed.conferences) if c is not None]


# --- Claude Code CLI backend ----------------------------------------------


def _claude_cli_path() -> str:
    """Locate the ``claude`` CLI or raise a helpful error."""
    path = shutil.which("claude")
    if not path:
        raise RuntimeError(
            "The 'claude' CLI was not found on PATH. Install Claude Code "
            "(https://docs.claude.com/claude-code) to use the default "
            "'claude-code' backend, or pass backend='api' to use the Anthropic "
            "API instead."
        )
    return path


def _run_claude_cli(
    prompt: str,
    *,
    append_system: Optional[str] = None,
    tools: Optional[List[str]] = None,
    json_schema: Optional[dict] = None,
    model: Optional[str] = None,
    timeout: int,
) -> dict:
    """Invoke ``claude -p`` headlessly and return the parsed JSON result payload.

    The subprocess is run with ``ANTHROPIC_API_KEY`` removed from its environment
    so the CLI authenticates via the user's Claude Code subscription rather than
    falling back to the metered API.
    """
    cmd = [_claude_cli_path(), "-p", prompt, "--output-format", "json"]
    if append_system:
        cmd += ["--append-system-prompt", append_system]
    if tools is not None:
        # An empty list disables all tools ("" per the CLI); a populated list
        # restricts the session to exactly those built-in tools and pre-approves
        # them so headless runs do not stall on a permission prompt.
        cmd += ["--tools", ",".join(tools)]
        if tools:
            cmd += ["--allowedTools", *tools]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if model:
        cmd += ["--model", model]

    env = {k: v for k, v in os.environ.items() if k != ANTHROPIC_API_KEY_ENV}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing dependent
        raise RuntimeError(f"claude CLI timed out after {timeout}s") from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"could not parse claude CLI output as JSON: {proc.stdout[:500]!r}"
        ) from exc
    if payload.get("is_error"):
        raise RuntimeError(f"claude CLI returned an error: {payload.get('result')}")
    return payload


def _research_via_cli(categories: List[str], model: Optional[str]) -> str:
    """Run the research phase through the headless ``claude`` CLI."""
    system = _RESEARCH_SYSTEM.format(seed_list=_seed_checklist(categories))
    payload = _run_claude_cli(
        _research_prompt(categories),
        append_system=system,
        tools=["WebSearch", "WebFetch"],
        model=model,
        timeout=_CLI_RESEARCH_TIMEOUT,
    )
    return payload.get("result", "")


def _extract_via_cli(research_text: str, model: Optional[str]) -> List[Conference]:
    """Run the extraction phase through the headless ``claude`` CLI."""
    payload = _run_claude_cli(
        research_text,
        append_system=_EXTRACT_SYSTEM,
        tools=[],  # extraction needs no tools
        json_schema=_ExtractedList.model_json_schema(),
        model=model,
        timeout=_CLI_EXTRACT_TIMEOUT,
    )
    data = payload.get("structured_output")
    if not data:
        return []
    parsed = _ExtractedList.model_validate(data)
    return [c for c in (_to_conference(item) for item in parsed.conferences) if c is not None]


# --- Public entry point ----------------------------------------------------


def discover_conferences(
    categories: Optional[Iterable[str]] = None,
    backend: str = DEFAULT_BACKEND,
    model: Optional[str] = None,
    max_tokens: int = 16000,
) -> List[Conference]:
    """Discover conferences for the given categories and return typed records.

    Args:
        categories: Fields to search (e.g. ``["radiology"]``). Defaults to
            ``["radiology"]``.
        backend: ``"claude-code"`` (default) drives the local ``claude`` CLI on
            the user's Claude Code subscription; ``"api"`` calls the Anthropic
            API directly (requires ``ANTHROPIC_API_KEY`` and credits).
        model: Model id to drive the agent. ``None`` uses
            :data:`config.ANTHROPIC_MODEL` for the API backend and Claude Code's
            configured model for the CLI backend.
        max_tokens: Output token ceiling per API call (API backend only).

    Returns:
        A list of ``Conference`` records.
    """
    if backend not in DISCOVERY_BACKENDS:
        raise ValueError(
            f"Unknown discovery backend {backend!r}; expected one of "
            f"{', '.join(DISCOVERY_BACKENDS)}."
        )

    cats = list(categories) if categories else ["radiology"]

    if backend == "claude-code":
        research_text = _research_via_cli(cats, model)
        if not research_text.strip():
            return []
        return _extract_via_cli(research_text, model)

    # backend == "api"
    import anthropic  # imported lazily so non-discovery code paths don't need it

    resolved_model = model or ANTHROPIC_MODEL
    client = anthropic.Anthropic()
    research_text = _research(client, cats, resolved_model, max_tokens)
    if not research_text.strip():
        return []
    return _extract(client, research_text, resolved_model, max_tokens)
