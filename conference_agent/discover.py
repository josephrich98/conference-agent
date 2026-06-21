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
import re
import shutil
import subprocess  # only used to invoke the resolved local `claude` CLI  # nosec B404
from datetime import date
from typing import Iterable, List, Optional

from pydantic import BaseModel, Field

from conference_agent.config import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_MODEL,
    SEED_CONFERENCES,
)
from conference_agent.models import (
    Conference,
    RemoteOption,
    normalize_formats,
    normalize_subcategories,
)

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

For each notable conference in the requested field, gather:
- acronym and full name
- the specific subcategory field(s) it belongs to -- a conference may span more
  than one (e.g. SPR is both radiology and pediatrics; MICCAI is both radiology
  and machine learning), so list every field that applies, comma-separated
- the most recent (prior) edition: abstract submission deadline, full paper /
  manuscript deadline, and the conference start and end dates
- the upcoming edition: abstract submission deadline, full paper / manuscript
  deadline, and the conference start and end dates
- the host city / venue (location) of each edition
- the official website URL
- the submission / presentation formats the conference accepts -- any of:
  abstract (a short abstract submission), paper (a full paper / manuscript),
  poster (a poster presentation), oral (an oral / podium presentation) -- listing
  every format that applies, comma-separated (most meetings take abstracts;
  proceedings venues such as NeurIPS or CVPR also take full papers; many accept
  both poster and oral presentations)
- whether it can be attended remotely (in-person, virtual, or hybrid)
- the registration cost: give the actual dollar figure(s) when available (e.g.
  "$1,095 member / $1,395 non-member, early-bird"), not just "varies"
- the registration period(s) as free text, for the prior and the upcoming edition
  separately (prior_registration / upcoming_registration): capture the windows the
  meeting publishes, e.g. "Early bird: Jan 5 - Mar 1; Regular: Mar 2 - conference"
  or, if only an opening is given, "Registration opens June 2026". Leave blank when
  no registration timing is published -- do not guess
- the typical annual attendance (total number of attendees) of the most recent
  edition, as a plain integer; plus the year that figure describes and the source
  URL you took it from. Prefer an official figure; state the number only when you
  find a credible source, and leave it unstated rather than guessing.

Abstract and paper deadlines matter as much as the meeting dates: check each \
conference's "Call for Abstracts", "Submit", or "Important Dates" page and report \
the abstract submission deadline and the full paper / manuscript deadline for \
both the prior and the upcoming edition. Many series publish these on a separate \
page from the meeting dates, so search for them explicitly. State each deadline \
date you find; only say a deadline is unannounced after looking for it.

The attendance figure is almost never on the conference home page, so search the \
web for it explicitly -- run queries like "<conference name> annual meeting \
attendance" or "<conference> <year> by the numbers" and follow the results off \
the official site. It usually appears in a post-meeting press release or wrap-up, \
an annual report, a "by the numbers" recap, an exhibitor/sponsor prospectus, or \
trade-press coverage (e.g. TSNN, Smart Meetings) rather than on the meeting's own \
landing page. Report the total attendee count for the most recent edition you can \
source, the year that figure describes, and the exact URL it came from. Give a \
number only when a source states it; do not estimate from venue size or registration \
fees, and leave it unstated if no credible figure turns up.
{attendance_hints}

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
not state; do not invent values. The subcategory field may list more than one \
field when a conference spans several (e.g. "radiology, machine learning"); \
separate the tags with commas. Capture every date the notes give: populate the \
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
early-bird"). The attendance field must be a plain integer count of attendees \
(digits only, no commas or words) or "" if the notes give no figure; \
attendance_year is the four-digit year that figure describes (or ""); \
attendance_source is the URL the figure came from (or ""). The remote_option \
field must be "in-person", "virtual", "hybrid", "unknown", or "". The formats \
field lists the submission/presentation formats the conference offers -- any of \
"abstract", "paper", "poster", "oral" -- comma-separated, or "" if the notes do \
not say; use "paper" only for a genuine full-paper / manuscript venue, not for an \
abstract-only meeting."""


class _ExtractedConference(BaseModel):
    """Flat, all-string extraction target.

    Structured outputs cap schema complexity, so the model returns plain strings
    (empty when unknown) rather than the richer ``Conference`` schema with its
    optional date/enum fields. We parse and validate into ``Conference`` below.
    """

    acronym: str
    name: str
    subcategory: str = Field(
        description="Specific field(s) the conference belongs to; comma-separate "
        'multiple, e.g. "radiology, machine learning"'
    )
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
    formats: str = Field(
        description="Submission/presentation formats offered, comma-separated, any of: "
        "abstract, paper, poster, oral (or '' if the notes do not say)"
    )
    cost: str
    attendance: str = Field(
        description="Total attendee count as a plain integer (digits only), or '' if unknown"
    )
    attendance_year: str = Field(
        description="Four-digit year the attendance figure describes, or ''"
    )
    attendance_source: str = Field(
        description="Source URL the attendance figure was taken from, or ''"
    )
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


def _parse_int(value: str) -> Optional[int]:
    """Parse an integer from a possibly-formatted string (e.g. "45,000"); None if not."""
    if not value:
        return None
    digits = re.sub(r"[,\s]", "", value.strip())
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def _to_conference(item: _ExtractedConference) -> Optional[Conference]:
    """Convert a flat extracted record into a typed ``Conference`` (or None)."""
    if not (item.acronym.strip() and item.name.strip() and item.subcategory.strip()):
        return None

    try:
        remote = RemoteOption(item.remote_option.strip().lower()) if item.remote_option.strip() else None
    except ValueError:
        remote = None

    return Conference(
        acronym=item.acronym.strip(),
        name=item.name.strip(),
        subcategories=normalize_subcategories(item.subcategory),
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
        formats=normalize_formats(item.formats),
        cost=_clean(item.cost),
        attendance=_parse_int(item.attendance),
        attendance_year=_parse_int(item.attendance_year),
        attendance_source=_clean(item.attendance_source),
        notes=_clean(item.notes),
    )


def _seed_checklist(subcategories: List[str]) -> str:
    """Bullet list of seed conferences in the requested subcategories (or all)."""
    subs = {s.strip().lower() for s in subcategories}
    lines = [
        f"- {acronym} — {name}"
        for acronym, name, subcategory in SEED_CONFERENCES
        if not subs or (set(normalize_subcategories(subcategory)) & subs)
    ]
    return "\n".join(lines) if lines else "- (no seeds for this subcategory)"


def _attendance_hints_block(hints: "Optional[dict]") -> str:
    """Render the 'last known attendance source' guidance for the research prompt.

    ``hints`` maps an acronym to ``{"source": url, "year": int|None}`` (see
    :func:`database.known_attendance_sources`). On a refresh this tells the model
    to re-check the URL a figure last came from -- and, when that URL is
    year-stamped, the next edition's URL -- before searching afresh. Returns an
    empty string when there are no hints, so a first-time run's prompt is unchanged.
    """
    if not hints:
        return ""
    lines = []
    for acronym in sorted(hints):
        entry = hints[acronym] or {}
        url = entry.get("source")
        if not url:
            continue
        year = entry.get("year")
        lines.append(f"- {acronym}{f' ({year})' if year else ''}: {url}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "A previously found attendance source is listed for some conferences below. "
        "On this refresh, check that exact URL FIRST; if it still states a current "
        "attendance figure, use it. If the URL contains an edition year (e.g. "
        "\".../2025/...\" or \"...2025...\"), also try the same URL with the year "
        "advanced to the next edition(s), since organizers often reuse the URL "
        "pattern. Only search the web afresh if neither yields a current figure. "
        "When you do, report the URL you actually used.\n"
        "Last known attendance sources:\n"
        f"{body}"
    )


def _research_prompt(subcategories: List[str]) -> str:
    """The user-facing research instruction, shared by both backends."""
    return (
        "Find the major conferences in the following "
        f"{'fields' if len(subcategories) > 1 else 'field'}: "
        f"{', '.join(subcategories)}. Search the web for current dates and details, "
        "then summarize each conference and its key dates."
    )


def _research(
    client, subcategories: List[str], model: str, max_tokens: int,
    attendance_hints: "Optional[dict]" = None,
) -> str:
    """Run the web-search agentic loop and return the model's research text."""
    system = _RESEARCH_SYSTEM.format(
        seed_list=_seed_checklist(subcategories),
        attendance_hints=_attendance_hints_block(attendance_hints),
    )
    prompt = _research_prompt(subcategories)
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
        proc = subprocess.run(  # cmd is a resolved CLI path + controlled flags, no shell  # nosec B603
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


def _research_via_cli(
    subcategories: List[str], model: Optional[str], attendance_hints: "Optional[dict]" = None
) -> str:
    """Run the research phase through the headless ``claude`` CLI."""
    system = _RESEARCH_SYSTEM.format(
        seed_list=_seed_checklist(subcategories),
        attendance_hints=_attendance_hints_block(attendance_hints),
    )
    payload = _run_claude_cli(
        _research_prompt(subcategories),
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
    subcategories: Optional[Iterable[str]] = None,
    backend: str = DEFAULT_BACKEND,
    model: Optional[str] = None,
    max_tokens: int = 16000,
    attendance_hints: "Optional[dict]" = None,
) -> List[Conference]:
    """Discover conferences for the given subcategories and return typed records.

    Args:
        subcategories: Fields to search (e.g. ``["radiology"]``). Defaults to
            ``["radiology"]``.
        backend: ``"claude-code"`` (default) drives the local ``claude`` CLI on
            the user's Claude Code subscription; ``"api"`` calls the Anthropic
            API directly (requires ``ANTHROPIC_API_KEY`` and credits).
        model: Model id to drive the agent. ``None`` uses
            :data:`config.ANTHROPIC_MODEL` for the API backend and Claude Code's
            configured model for the CLI backend.
        max_tokens: Output token ceiling per API call (API backend only).
        attendance_hints: Optional ``{acronym: {"source": url, "year": int}}`` map
            of previously found attendance sources (see
            :func:`database.known_attendance_sources`). On a refresh, the research
            prompt re-checks these URLs (and their year-bumped successors) before
            searching afresh, so a known figure's source is reused.

    Returns:
        A list of ``Conference`` records.
    """
    if backend not in DISCOVERY_BACKENDS:
        raise ValueError(
            f"Unknown discovery backend {backend!r}; expected one of "
            f"{', '.join(DISCOVERY_BACKENDS)}."
        )

    subs = list(subcategories) if subcategories else ["radiology"]

    if backend == "claude-code":
        research_text = _research_via_cli(subs, model, attendance_hints)
        if not research_text.strip():
            return []
        return _extract_via_cli(research_text, model)

    # backend == "api"
    import anthropic  # imported lazily so non-discovery code paths don't need it

    resolved_model = model or ANTHROPIC_MODEL
    client = anthropic.Anthropic()
    research_text = _research(client, subs, resolved_model, max_tokens, attendance_hints)
    if not research_text.strip():
        return []
    return _extract(client, research_text, resolved_model, max_tokens)
