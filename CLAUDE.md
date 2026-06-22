# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

`conference_agent` is an AI agent that automatically compiles a curated table of
major academic and professional conferences and exports their deadlines and
dates as a subscribable calendar feed. For each conference series the agent
records its **subcategory** tags (one or more granular fields per series — e.g.
SPR is both radiology and pediatrics, MICCAI is radiology and machine learning)
and a derived **category** (one of ten fixed top-level buckets: humanities,
social science, medicine, biology, chemistry, physics, math, stats, computer
science, artificial intelligence — computed from the subcategories via
`models.SUBCATEGORY_TO_CATEGORY`, never hand-set), its **prior** and
**upcoming** editions (abstract deadline, paper deadline, conference dates for
each, plus a free-text registration field per edition capturing the registration
window(s) — e.g. "Early bird: Jan 5 – Mar 1; Regular: Mar 2 – conference" — since
registration is published as periods, not a single date), a derived conference
month, abstract month, and paper
month (each
taken from the matching date so rows sort by season even when their years are
offset; registration, being free text, has no derived month), the official
link, remote-attendance option, cost, a sourced annual-attendance figure, and a
size bucket derived deterministically from that figure (large / medium / small,
an objective proxy for prominence — e.g. RSNA ≈ 45,000 attendees = large). It
normalizes that information into a typed schema, stores it in a
SQL database, exposes it through a boolean-searchable web table, and serves each
conference's deadlines and dates as a credential-free iCalendar (`.ics`) feed.
Discovery is seeded
across medicine (radiology and ~18 other specialties), genomics/bioinformatics,
and data science; the seed list (`SEED_CONFERENCES` in `config.py`) is the lever
for adding fields, the standing refresh subcategories are derived from it, and
`TAXONOMY.md` documents the field map and cadence policy.

## Status

Core pipeline implemented: typed schema, SQLAlchemy persistence with idempotent
upserts, the LLM discovery agent (Anthropic web search + structured output), a
credential-free iCalendar (`.ics`) feed, an email notifier, and a web table
interface (boolean search + per-row calendar download). See `## Architecture`
for the design.

## Repository Layout

- `conference_agent/` — core reusable package
  - `models.py` — `Conference` pydantic schema (one record per conference *series*,
    holding prior + upcoming editions, a list of `subcategories` tags, a controlled
    list of submission/presentation `formats` (any of abstract / paper / poster /
    oral), an `attendance`
    figure, and derived `categories` / `category` (the broad bucket), `conference_month`
    / `abstract_month` / `paper_month` (registration is free text — `registration`
    property, no derived month) and
    `size` / `attendance_display` / `format` / `subcategory` properties);
    `ConferenceSize` and `RemoteOption` enums; `CATEGORIES` and
    `SUBCATEGORY_TO_CATEGORY` (the top-level vocabulary + derivation map);
    `CONFERENCE_FORMATS` vocabulary; `size_for_attendance` (the size-bucketing rule),
    `categories_for_subcategories` (the category derivation), `normalize_subcategories`
    (the shared tag parser), and `normalize_formats` (the format-vocabulary parser)
  - `config.py` — constants, controlled vocabularies, seed list, Anthropic model
    id, and SMTP / notification settings
  - `discover.py` — the AI discovery agent: web search (research) + structured
    output (extraction) to find conferences and extract typed fields. Two
    interchangeable backends run that flow: `claude-code` (default) drives the
    local `claude` CLI on the user's Claude Code subscription (the subprocess
    runs with `ANTHROPIC_API_KEY` removed so it never falls back to the metered
    API); `api` calls the Anthropic API directly and requires `ANTHROPIC_API_KEY`
  - `database.py` — SQLAlchemy ORM + idempotent ingestion/query helpers
  - `calendar_sync.py` — iCalendar (`.ics`) feed builder (RFC 5545; stable,
    deterministic event ids so a re-fetched feed updates events in place)
  - `refresh.py` — per-conference auto-check policy: decides which series are
    "due" for re-discovery (6–12-month staleness window, biweekly re-check via a
    `last_checked` column) so `daily_update.py --cadence due` targets only them
  - `notify.py` — email summary after a discovery / daily refresh
  - `cli.py` — command-line entry point (`discover` / `seed` / `list` / `serve`)
- `web/` — FastAPI app + static single-page table (`search.py` boolean-query
  language, `nl_query.py` optional natural-language → boolean-query translation
  via a local Ollama model, `app.py` REST API, `static/index.html`, `handler.py`
  Lambda entry point, `requirements.txt` for the Lambda build). For the
  credential-free static deployment, `static/search.js` and `static/calendar.js`
  reimplement `search.py` and `calendar_sync.py` in the browser (see the
  static-hosting design decision below)
- `scripts/` — runnable entry points (`build_table.py`, `daily_update.py`,
  `push_db.py`, `deploy.sh` one-command reconcile + deploy, `scheduled_discovery.sh`
  the biweekly cron job, `build_static.py` the static-site bundler)
- `infra/` — AWS SAM deployment (`template.yaml`: CloudFront over an
  IAM-protected Lambda Function URL + RDS PostgreSQL in a VPC, with optional
  `DomainName`/`AcmCertificateArn` for a custom domain; `samconfig.toml`); built
  via the root `Makefile`
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
pip install -e ".[web]"            # FastAPI web table + calendar feed
```

`pyproject.toml` is the authoritative source for dependencies. Add new
dependencies there rather than installing ad hoc.

### Credentials (not committed)

- `ANTHROPIC_API_KEY` — required only for the `api` discovery backend (and the
  CI refresh workflows, which pass `--backend api`). The default `claude-code`
  backend uses the local Claude Code subscription instead and needs no key.

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
- **Controlled vocabularies.** `ConferenceSize` (`large`/`medium`/`small`) and
  `RemoteOption` (`in-person`/`virtual`/`hybrid`/`unknown`) are enums, not free
  text, so the table and queries can filter/color consistently.
- **Two-level classification: derived category over free-form subcategory.** The
  granular `subcategory` is the one free-form categorical column (the specific
  field, multi-valued). The broad `category` is one or more of ten fixed buckets
  (`models.CATEGORIES`) and is *derived*, never stored as input: every subcategory
  maps to exactly one category via `models.SUBCATEGORY_TO_CATEGORY`, and a series'
  category is the de-duplicated set of its subcategories' buckets (so MICCAI →
  `medicine, artificial intelligence`). Like `size`, the stored `category` column
  is only ever written by the derivation, so it cannot drift from the
  subcategories; `recompute_categories` re-derives all rows if the map changes. A
  new subcategory needs a `SUBCATEGORY_TO_CATEGORY` entry (a test enforces seed
  coverage). The legacy single `category` column (granular tags) is renamed to
  `subcategory` in place on first open by `database._migrate_category_to_subcategory`.
- **Deterministic, sourced size (not a subjective reputation label).** Size is a
  computed property, never a stored hand-set value: `models.size_for_attendance`
  buckets the `attendance` integer (≥1,000 → large, ≥100 → medium, else small;
  blank when attendance is unknown — thresholds live in `models.py`). The stored `size` column is only ever written
  by that function, so it can never drift from the figure. Each attendance figure
  carries the year it describes and the source URL it came from (provenance kept
  internal — not exposed via the API/CSV); the table shows it as e.g.
  `45,000 (2025)`.
- **SQLAlchemy over raw SQL.** The same ORM runs against SQLite (local) or any
  SQLAlchemy backend with only a connection-string change.
- **Idempotent ingestion.** Upserts key on the conference id (the acronym), so
  re-running discovery updates rather than duplicates rows.
- **Idempotent calendar feed.** Each event carries a deterministic id derived
  (base32hex) from the conference id and event kind, so a re-fetched feed updates
  existing events instead of creating duplicates. A conference yields up to three
  events for its upcoming edition: abstract deadline, paper deadline, and the
  conference dates. The feed is pure-Python iCalendar (RFC 5545), so it needs no
  credentials and runs from the static/Lambda web layer. All-day reminders are
  anchored to the morning (see `calendar_sync._alarm_trigger`) so calendar apps
  label "N days before" correctly rather than a day early.
- **Boolean-searchable web table.** `web/` mirrors a proven pattern: a small
  boolean query language (`field:value`, `AND`/`OR`/`NOT`, parentheses, date
  comparisons) compiled to SQLAlchemy filters, a FastAPI JSON/CSV API, and a
  static single-page table with a per-row "📅 cal" calendar-download button and a
  "Subscribe (.ics)" feed URL that mirrors the active search.
- **Optional natural-language search over a local LLM.** `web/nl_query.py`
  translates a plain-English request into the boolean query language above using
  a free, local Ollama model (no API key, no external network call). The system
  prompt's field list and controlled vocabularies are derived from
  `web.search.field_help`, so they never drift from what the parser accepts; the
  model's output is validated with `build_filter` (with one repair round) before
  it is returned, so the search box is never populated with a query that errors.
  The feature is optional and degrades gracefully — `GET /api/translate` returns
  503 when no model is running, and the manual boolean box still works. It is a
  web-layer concern only, so the core `conference_agent` package takes no new
  dependency (the Ollama HTTP call uses the standard library).
- **Separation of concerns.** Discovery, persistence, the calendar feed, email
  notification, and the web layer are independent modules; each can run on its
  own schedule.
- **Static, compute-free hosting path.** The deployed site is read-only
  (discovery/ingestion run offline), so it can be served as static files with no
  per-request compute — there is no Lambda to invoke and no database to keep
  running, so query volume cannot incur cost. `scripts/build_static.py` snapshots
  the database to `dist/data/conferences.json` and copies the single-page UI into
  `dist/`; the browser does search, sort, CSV export, and per-row `.ics`
  generation entirely client-side. `web/static/search.js` ports the boolean query
  language from `web/search.py` (compiling to a row predicate instead of a SQL
  filter), and `web/static/calendar.js` ports `conference_agent/calendar_sync.py`
  (matching its base32hex UIDs so re-downloaded events stay idempotent). The risk
  of the two search implementations drifting is guarded by
  `tests/test_search_parity.py`, which asserts the JS and Python searches select
  identical rows over a query corpus (skipped when `node` is absent, e.g. in CI).
  The static bundle omits the whole-list subscribe feed (needs per-request
  compute); per-row calendar downloads and manual boolean search are unaffected.
  Natural-language ("AI") search *is* available on the static site:
  `web/static/nl_query.js` ports the prompt and validate/repair loop from
  `web/nl_query.py` but runs a small instruction model entirely in the browser
  over WebGPU via WebLLM (no API key, no backend, no egress after a one-time
  ~1 GB model download), so it stays within the compute-free, credential-free
  design. It loads lazily on first use, reuses the snapshot's `fields` catalog
  for the prompt and `search.js`'s parser for validation, and degrades to the
  boolean box when WebGPU is unavailable. (The server `web/nl_query.py` +
  `/api/translate` Ollama path remains for the dynamic backend.) See `DEPLOY.md`.
  The AWS SAM stack remains as the alternative dynamic backend.

## CI/CD

- `ci.yml`: `ruff check` + `pytest -m "not network and not llm"` on push/PR.
  Committed tests are offline; mark any test that hits the network with
  `@pytest.mark.network` and any test that calls the Anthropic API with
  `@pytest.mark.llm` so CI stays hermetic.

## Deployment

- **"Deploy" means the static bundle to Vercel (the live path), not AWS.** When
  the user asks to deploy, build and ship the static site:

  ```bash
  python scripts/build_static.py                 # snapshot DB + UI into dist/
  ( cd dist && npx vercel deploy --prod --yes )  # publish to the conferenceagent project
  ```

  The live site is `https://conferenceagent.vercel.app`, served as static files
  directly from Vercel — no per-request compute and no AWS in the request path.
  `dist/` is linked (via `dist/.vercel/project.json`) to the Vercel project
  **conferenceagent**; deploy only from `dist/`. Cloudflare Pages
  (`npx wrangler pages deploy dist`) is an equivalent static host if ever needed.
  To refresh the live data, re-run `build_static.py` after a discovery run and
  redeploy — there is no database to push.
- **Legacy AWS path (no longer the deploy target).** `scripts/deploy.sh` (push
  local DB → RDS; `DEPLOY_CODE=1` also `sam build` + `aws lambda
  update-function-code`) and the `deploy/vercel/` proxy (a rewrite to CloudFront)
  belong to the retired AWS stack documented in `DEPLOY.md`. Do **not** run them
  for a normal deploy — `deploy/vercel/` is linked to the same Vercel project, so
  deploying from it would revert the public URL to proxying AWS. Use them only
  when explicitly working on the AWS stack.
- **Automatic refresh.** `scripts/scheduled_discovery.sh` (the biweekly cron job)
  hashes `data/conferences.db` before and after the `daily_update.py --cadence
  due` run and redeploys only when the DB actually changed, so the live site
  tracks new discoveries without a manual step. (Update its deploy step to the
  Vercel static path above if it still calls `scripts/deploy.sh`.)

## Conventions

- Generated databases/tables (`*.db`, `*.sqlite`, `*.sql`) and the `data/`
  directory are gitignored and must never be committed.
- Secrets (`ANTHROPIC_API_KEY`, SMTP credentials) are never committed and never
  hard-coded.
- Use American English spelling. Write with a professional tone. Do not overstate
  claims.

## Working with the Anthropic API

When implementing or modifying `discover.py` (or any code that calls Claude),
consult the `claude-api` skill for current model ids, tool-use patterns, and
structured-output guidance rather than relying on memory. The model id is
centralized in `conference_agent/config.py`.
