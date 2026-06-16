# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

`conference_agent` is an AI agent that automatically compiles a curated table of
major academic and professional conferences and keeps it in sync with Google
Calendar. The agent discovers conferences and their key dates (abstract /
submission deadlines, conference dates), location, attendance/submission
requirements, official link, and an importance tier (e.g. RSNA = big,
SPR = medium), normalizes that information into a typed schema, stores it in a
SQL database, and pushes each conference (and its deadlines) into Google
Calendar as events.

## Status

Early scaffolding. The package layout, schema, and module boundaries are in
place as stubs; the discovery agent, database layer, and calendar sync are not
yet implemented. See `## Planned Architecture` for the intended design.

## Repository Layout

- `conference_agent/` — core reusable package
  - `models.py` — `Conference` pydantic schema (one record per conference edition)
  - `config.py` — constants, controlled vocabularies (`ConferenceTier`), default
    conference seed lists, and the Anthropic model id used by the agent
  - `discover.py` — the AI discovery agent: drives the Anthropic API with
    tool-use / web search to find conferences and extract their structured fields
  - `database.py` — SQLAlchemy ORM + idempotent ingestion/query helpers
  - `calendar_sync.py` — Google Calendar sync (OAuth, event upsert, dedupe)
  - `cli.py` — command-line entry point
- `scripts/` — runnable CLI entry points (`build_table.py`, `sync_calendar.py`)
- `tests/` — offline unit tests (network/LLM tests are marked and excluded from CI)
- `data/` — generated tables / databases (gitignored, never committed)
- `.github/workflows/` — `ci.yml` (lint + offline tests)

## Development Setup

Use the `conference_agent` conda environment for all work in this repo:

```bash
conda activate conference_agent
```

```bash
pip install -e ".[dev]"            # core + test tooling
pip install -e ".[discover]"       # add fetch/parse helpers for the agent
pip install -e ".[calendar]"       # add the Google Calendar client libraries
```

`pyproject.toml` is the authoritative source for dependencies. Add new
dependencies there rather than installing ad hoc.

### Credentials (not committed)

- `ANTHROPIC_API_KEY` — environment variable for the discovery agent.
- Google Calendar OAuth — a `credentials.json` (OAuth client secret) downloaded
  from Google Cloud Console; the first run produces a cached `token.json`. Both
  files are gitignored and must never be committed.

## Planned Architecture / Key Design Decisions

- **One record per conference edition.** The `Conference` schema keys on
  acronym + year (e.g. `RSNA-2026`), so a recurring conference produces a new
  row each year rather than mutating last year's dates.
- **LLM-driven discovery, typed output.** `discover.py` uses the Anthropic API
  with web search / tool use to find conferences, then returns data conforming
  to the `Conference` pydantic model. The model id lives in `config.py`
  (default: the latest Claude model) so it is changed in one place.
- **Controlled importance tier.** `ConferenceTier` (`big` / `medium` / `small`)
  is an enum, not free text, so calendars and views can filter/color consistently.
- **SQLAlchemy over raw SQL.** The same ORM models run against SQLite (local)
  and any other SQLAlchemy-supported backend with only a connection-string change.
- **Idempotent ingestion.** Upserts key on the conference id (acronym + year),
  so re-running discovery updates rather than duplicates rows.
- **Idempotent calendar sync.** Calendar events carry a stable external id
  derived from the conference id so re-syncing updates existing events instead of
  creating duplicates. Each conference yields up to two events: the abstract /
  submission deadline and the conference dates.
- **Separation of concerns.** Discovery (find/extract), persistence (database),
  and sync (calendar) are independent modules; each can run on its own schedule.

## CI/CD

- `ci.yml`: `ruff check` + `pytest -m "not network and not llm"` on push/PR.
  Committed tests are offline; mark any test that hits the network with
  `@pytest.mark.network` and any test that calls the Anthropic API with
  `@pytest.mark.llm` so CI stays hermetic.

## Conventions

- Generated databases/tables (`*.db`, `*.sqlite`, `*.sql`) and the `data/`
  directory are gitignored and must never be committed.
- Secrets (`ANTHROPIC_API_KEY`, `credentials.json`, `token.json`) are never
  committed and never hard-coded.
- Use American English spelling. Write with a professional tone. Do not overstate
  claims.

## Working with the Anthropic API

When implementing or modifying `discover.py` (or any code that calls Claude),
consult the `claude-api` skill for current model ids, tool-use patterns, and
structured-output guidance rather than relying on memory. The model id is
centralized in `conference_agent/config.py`.
