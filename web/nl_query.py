"""Natural-language → boolean-query translation via a local LLM.

Turns a plain-English request ("big virtual radiology conferences with an
abstract deadline after June") into the boolean query language understood by
:mod:`web.search` (``subcategory:radiology AND size:large AND remote:virtual AND
abstract_due:>=2026-06``), so a user can search without learning the syntax.

The translation runs against a free, local `Ollama <https://ollama.com>`_ server
(no API key, no network egress) with a small instruction model. The feature is
optional and degrades gracefully: if the server is unreachable, callers get an
:class:`LLMUnavailable` error and the web UI falls back to the manual boolean
box.

Design notes
------------
- **The field catalog is derived, never hand-copied.** The system prompt lists
  the queryable fields and their controlled vocabularies straight from
  :func:`web.search.field_help`, so adding a field or vocabulary value updates
  the prompt automatically.
- **Output is validated, with one repair round.** The model's query is compiled
  with :func:`web.search.build_filter`; if it does not parse, the parse error is
  fed back for a single correction attempt. A query that still fails to parse is
  reported rather than returned, so the search box is never populated with a
  string that would error.
- **Zero new dependencies.** The Ollama HTTP API is called with the standard
  library (:mod:`urllib`), matching the project's pure-stdlib calendar/web layer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from conference_agent import config
from web.search import QueryError, build_filter, field_help


class LLMUnavailable(RuntimeError):
    """Raised when the local LLM server cannot be reached or errors out."""


class TranslationError(RuntimeError):
    """Raised when the model's output cannot be turned into a valid query."""


@dataclass
class Translation:
    """The result of translating natural language to a boolean query."""

    query: str  # the compiled, validated boolean query
    natural_language: str  # the original request, echoed back
    repaired: bool = False  # True if a repair round was needed


def _field_reference() -> str:
    """A compact, human-readable field reference built from the search registry.

    Mirrors exactly the fields the parser accepts (and their controlled
    vocabularies), so the prompt can never drift from what ``build_filter`` will
    actually accept.
    """
    lines = []
    for entry in field_help()["fields"]:
        lines.append(f"- {entry['field']} ({entry['type']})")
    return "\n".join(lines)


# The translation contract. Kept terse: small local models follow short, concrete
# rules with a few examples far better than long prose. The field list and
# vocabularies are injected from the live search registry.
_SYSTEM_PROMPT = """\
You translate a user's plain-English description of conferences they want into a \
single boolean search query. Output ONLY JSON: {{"query": "<query>"}}.

The query language (mirrors the table's columns):
- Scoped match: field:value (case-insensitive substring). Quote multi-word \
values: subcategory:"machine learning".
- Boolean operators: AND, OR, NOT, with parentheses for grouping. Adjacent terms \
are implicitly AND-ed.
- Date fields accept YYYY, YYYY-MM, or YYYY-MM-DD with operators > >= < <= = \
attached after the colon: abstract_due:>=2026-06, conference_dates:2027.
- Month fields take 1-12 or a month name: conference_month:november, \
abstract_month:>=6.
- Presence test: field:* means the field is set; NOT field:* means it is unset.

Queryable fields (categorical fields list their allowed values after "cat:"):
{fields}

Guidance:
- Use `subcategory` for a specific field (radiology, oncology, "machine \
learning", genomics, ...). Use `category` only for the broad buckets listed for \
it (medicine, computer science, artificial intelligence, ...).
- Map size words to the `size` vocabulary: big/large→large, mid-size→medium, \
small→small.
- Map attendance/format/remote words to their controlled vocabularies.
- "deadline" alone means abstract_due. "no/without X" means NOT X:*.
- Time of year WITHOUT a specific year — "in November", "in the fall", "any \
year", a month range like "September through January" — uses a MONTH field \
(conference_month / abstract_month / paper_month), never a date field. Use a \
date field (conference_dates / abstract_due / paper_due) ONLY when a specific \
year is named ("in 2027", "after June 2026"). Never invent a year that the \
request did not state.
- A range is two bounds. A range that stays within the year — "March to June" — \
uses AND: (conference_month:>=3 AND conference_month:<=6). A range that wraps \
past December — "September through January" — uses OR: (conference_month:>=9 OR \
conference_month:<=1).
- Use only the fields and values listed above. If the request names no usable \
filter, return {{"query": ""}} (an empty query matches everything).

Examples:
Request: big radiology conferences that are virtual
{{"query": "subcategory:radiology AND size:large AND remote:virtual"}}
Request: machine learning conferences with an abstract deadline after June 2026
{{"query": "subcategory:\\"machine learning\\" AND abstract_due:>=2026-06"}}
Request: cardiology or oncology meetings happening in 2027
{{"query": "(subcategory:cardiology OR subcategory:oncology) AND conference_dates:2027"}}
Request: genomics conferences in November with no paper deadline
{{"query": "subcategory:genomics AND conference_month:november AND NOT paper_due:*"}}
Request: big radiology conferences between September and January of any year
{{"query": "size:large AND subcategory:radiology AND (conference_month:>=9 OR conference_month:<=1)"}}
Request: oncology conferences held from March to June
{{"query": "subcategory:oncology AND (conference_month:>=3 AND conference_month:<=6)"}}
"""


def _build_messages(natural_language: str, prior_error: str | None = None) -> list[dict]:
    system = _SYSTEM_PROMPT.format(fields=_field_reference())
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Request: {natural_language}"},
    ]
    if prior_error:
        # One repair round: hand the model its own bad query's parse error.
        messages.append(
            {
                "role": "user",
                "content": (
                    f"That query failed to parse with error: {prior_error}. "
                    "Return corrected JSON using only the listed fields and values."
                ),
            }
        )
    return messages


def _call_ollama(messages: list[dict], *, model: str, base_url: str, timeout: float) -> str:
    """POST a chat completion to the local Ollama server; return the raw content."""
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",  # constrain the model to emit a JSON object
        "options": {"temperature": 0},  # deterministic translation
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local Ollama URL)  # nosec B310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LLMUnavailable(
            f"Could not reach the local LLM at {base_url}. Is Ollama running "
            f"(`ollama serve`) with the {model!r} model pulled? ({exc})"
        ) from exc
    except (TimeoutError, OSError) as exc:  # socket timeout, connection reset, ...
        raise LLMUnavailable(f"Local LLM request failed: {exc}") from exc

    content = (body.get("message") or {}).get("content")
    if not content:
        raise LLMUnavailable("Local LLM returned an empty response.")
    return content


def _extract_query(content: str) -> str:
    """Pull the ``query`` string out of the model's JSON response."""
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Model did not return valid JSON: {content!r}") from exc
    query = obj.get("query", "")
    if not isinstance(query, str):
        raise TranslationError(f"Model returned a non-string query: {query!r}")
    return query.strip()


def translate(
    natural_language: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> Translation:
    """Translate ``natural_language`` into a validated boolean query.

    Raises :class:`LLMUnavailable` if the local model cannot be reached and
    :class:`TranslationError` if its output cannot be coerced into a query that
    :func:`web.search.build_filter` accepts (after one repair round).
    """
    text = (natural_language or "").strip()
    if not text:
        return Translation(query="", natural_language="")

    model = model or config.NL_QUERY_MODEL
    base_url = base_url or config.OLLAMA_BASE_URL
    timeout = timeout if timeout is not None else config.NL_QUERY_TIMEOUT

    content = _call_ollama(_build_messages(text), model=model, base_url=base_url, timeout=timeout)
    query = _extract_query(content)

    # Validate against the real parser. An empty query is valid (matches all).
    try:
        if query:
            build_filter(query)
        return Translation(query=query, natural_language=text)
    except QueryError as first_error:
        # One repair round: feed the parse error back to the model.
        content = _call_ollama(
            _build_messages(text, prior_error=str(first_error)),
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        query = _extract_query(content)
        try:
            if query:
                build_filter(query)
        except QueryError as second_error:
            raise TranslationError(
                f"Could not produce a valid query (last error: {second_error})."
            ) from second_error
        return Translation(query=query, natural_language=text, repaired=True)
