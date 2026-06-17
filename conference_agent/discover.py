"""Conference discovery agent.

Drives the Anthropic API with the server-side web-search tool to find major
conferences in a category, then extracts the results into typed ``Conference``
records using structured outputs.

The flow is two-phase, which keeps each step simple and robust:

1. **Research** — an agentic loop with the ``web_search`` server tool. The model
   searches the web for the leading conferences in each category and writes up
   what it found (names, dates, deadlines, cost, links).
2. **Extract** — a single ``messages.parse`` call (no tools) that turns that
   research text into a validated list of ``Conference`` objects via a JSON
   schema derived from the pydantic model.

The model id is centralized in ``conference_agent.config.ANTHROPIC_MODEL``.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, List, Optional

from pydantic import BaseModel

from conference_agent.config import ANTHROPIC_MODEL, SEED_CONFERENCES, normalize_reputation
from conference_agent.models import Conference, ConferenceTier, RemoteOption

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


def _research(client, categories: List[str], model: str, max_tokens: int) -> str:
    """Run the web-search agentic loop and return the model's research text."""
    system = _RESEARCH_SYSTEM.format(seed_list=_seed_checklist(categories))
    prompt = (
        "Find the major conferences in the following "
        f"{'categories' if len(categories) > 1 else 'category'}: "
        f"{', '.join(categories)}. Search the web for current dates and details, "
        "then summarize each conference and its key dates."
    )
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


def discover_conferences(
    categories: Optional[Iterable[str]] = None,
    model: str = ANTHROPIC_MODEL,
    max_tokens: int = 16000,
) -> List[Conference]:
    """Discover conferences for the given categories and return typed records.

    Args:
        categories: Fields to search (e.g. ``["radiology"]``). Defaults to
            ``["radiology"]``.
        model: Anthropic model id to drive the agent.
        max_tokens: Output token ceiling per API call.

    Returns:
        A list of ``Conference`` records. Requires ``ANTHROPIC_API_KEY`` in the
        environment.
    """
    import anthropic  # imported lazily so non-discovery code paths don't need it

    cats = list(categories) if categories else ["radiology"]
    client = anthropic.Anthropic()

    research_text = _research(client, cats, model, max_tokens)
    if not research_text.strip():
        return []
    return _extract(client, research_text, model, max_tokens)
