# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

`conference_agent` is an AI agent that automatically compiles a curated table of
major academic and professional conferences and keeps it in sync with Google
Calendar. For each conference series the agent records its category (e.g.
radiology), its **prior** and **upcoming** editions (abstract deadline, paper
deadline, and conference dates for each), the official link, remote-attendance
option, cost, and a reputability tier (e.g. RSNA = big, SPR = medium). It
normalizes that information into a typed schema, stores it in a SQL database,
exposes it through a boolean-searchable web table, and pushes each conference's
upcoming deadlines and dates into Google Calendar as events. Discovery is seeded
across medicine (radiology and ~18 other specialties), genomics/bioinformatics,
and data science; the seed list (`SEED_CONFERENCES` in `config.py`) is the lever
for adding fields, the standing refresh categories are derived from it, and
`TAXONOMY.md` documents the field map and cadence policy.

## Status

Core pipeline implemented: typed schema, SQLAlchemy persistence with idempotent
upserts, the LLM discovery agent (Anthropic web search + structured output),
Google Calendar sync, an email notifier, and a web table interface (boolean
search + per-row calendar sync). See `## Architecture` for the design.

## Repository Layout

- `conference_agent/` — core reusable package
  - `models.py` — `Conference` pydantic schema (one record per conference *series*,
    holding prior + upcoming editions); `ConferenceTier` and `RemoteOption` enums
  - `config.py` — constants, controlled vocabularies, seed list, Anthropic model
    id, and SMTP / notification settings
  - `discover.py` — the AI discovery agent: web search (research) + structured
    output (extraction) to find conferences and extract typed fields. Two
    interchangeable backends run that flow: `claude-code` (default) drives the
    local `claude` CLI on the user's Claude Code subscription (the subprocess
    runs with `ANTHROPIC_API_KEY` removed so it never falls back to the metered
    API); `api` calls the Anthropic API directly and requires `ANTHROPIC_API_KEY`
  - `database.py` — SQLAlchemy ORM + idempotent ingestion/query helpers
  - `calendar_sync.py` — Google Calendar sync (OAuth, event upsert, dedupe)
  - `refresh.py` — per-conference auto-check policy: decides which series are
    "due" for re-discovery (6–12-month staleness window, biweekly re-check via a
    `last_checked` column) so `daily_update.py --cadence due` targets only them
  - `notify.py` — email summary after a discovery / daily refresh
  - `cli.py` — command-line entry point (`discover` / `list` / `sync` / `serve`)
- `web/` — FastAPI app + static single-page table (`search.py` boolean-query
  language, `app.py` REST API, `static/index.html`, `handler.py` Lambda entry
  point, `requirements.txt` for the Lambda build)
- `scripts/` — runnable entry points (`build_table.py`, `sync_calendar.py`,
  `daily_update.py`)
- `infra/` — AWS SAM deployment (`template.yaml`: Lambda + Function URL + RDS
  PostgreSQL in a VPC; `samconfig.toml`); built via the root `Makefile`
- `tests/` — offline unit tests (network/LLM tests are marked and excluded from CI)
- `data/` — generated tables / databases (gitignored, never committed)
- `.github/workflows/` — `ci.yml` (lint + offline tests), `weekly_update.yml`
  (flagship fields) and `monthly_update.yml` (remaining fields); both run
  `daily_update.py --cadence ...` with crons disabled by default, manually
  dispatchable
- `TAXONOMY.md` — the field taxonomy (domains → fields → flagship seeds) and the
  refresh-cadence policy
- `DEPLOY.md` — AWS deployment walkthrough (FastAPI + Lambda + PostgreSQL)

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

- `ANTHROPIC_API_KEY` — required only for the `api` discovery backend (and the
  CI refresh workflows, which pass `--backend api`). The default `claude-code`
  backend uses the local Claude Code subscription instead and needs no key.
- Google Calendar OAuth — a `credentials.json` (OAuth client secret) downloaded
  from Google Cloud Console; the first run produces a cached `token.json`. Both
  files are gitignored and must never be committed.

## Architecture / Key Design Decisions

- **One record per conference series.** The `Conference` schema keys on the
  acronym (e.g. `RSNA`) and holds both the **prior** and **upcoming** editions
  (abstract deadline, paper deadline, start/end dates for each). Re-running
  discovery updates the same row each cycle, rolling a newly announced edition
  into the "upcoming" columns rather than creating a second row. Keeping prior
  dates alongside upcoming lets the table show last year's schedule as a
  reference before next year's is announced.
- **LLM-driven discovery, typed output.** `discover.py` runs two phases: a
  web-search agentic loop (research) followed by a structured-output call
  (extraction) that validates against the `Conference` model. The same flow runs
  through either backend — the `api` backend uses `messages.parse`; the
  `claude-code` backend uses the CLI's `--json-schema` structured output. The
  `api` model id lives in `config.py` (default: the latest Claude model); the
  `claude-code` backend defaults to Claude Code's configured model.
- **Controlled vocabularies.** `ConferenceTier` (`big`/`medium`/`small`,
  reputability) and `RemoteOption` (`in-person`/`virtual`/`hybrid`/`unknown`) are
  enums, not free text, so the table and queries can filter/color consistently.
- **SQLAlchemy over raw SQL.** The same ORM runs against SQLite (local) or any
  SQLAlchemy backend with only a connection-string change.
- **Idempotent ingestion.** Upserts key on the conference id (the acronym), so
  re-running discovery updates rather than duplicates rows.
- **Idempotent calendar sync.** Each event carries a deterministic id derived
  (base32hex) from the conference id and event kind, so re-syncing updates
  existing events instead of creating duplicates. A conference yields up to three
  events for its upcoming edition: abstract deadline, paper deadline, and the
  conference dates.
- **Boolean-searchable web table.** `web/` mirrors a proven pattern: a small
  boolean query language (`field:value`, `AND`/`OR`/`NOT`, parentheses, date
  comparisons) compiled to SQLAlchemy filters, a FastAPI JSON/CSV API, and a
  static single-page table with a per-row "sync to Google Calendar" button.
- **Separation of concerns.** Discovery, persistence, calendar sync, email
  notification, and the web layer are independent modules; each can run on its
  own schedule.

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
