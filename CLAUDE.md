# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

`conference_agent` is an AI agent that automatically compiles a curated table of
major academic and professional conferences and exports their deadlines and
dates as a subscribable calendar feed. For each conference series the agent
records its **subcategory** tags (one or more granular fields per series — e.g.
SPR is both radiology and pediatrics, MICCAI is radiology and machine learning)
and a derived **category** (one of ten fixed top-level buckets: humanities,
social science, medicine, biology, chemistry, physics, mathematics, stats, computer
science, artificial intelligence — computed from the subcategories via
`models.SUBCATEGORY_TO_CATEGORY`, never hand-set), its **prior** and
**upcoming** editions (abstract deadline, late abstract deadline, paper
deadline, conference dates for
each, plus a free-text registration field per edition capturing the registration
window(s) — e.g. "Early bird: Jan 5 – Mar 1; Regular: Mar 2 – conference" — since
registration is published as periods, not a single date), a derived conference
month, abstract month, late abstract month, and paper
month (each
taken from the matching date so rows sort by season even when their years are
offset; registration, being free text, has no derived month), the official
link, remote-attendance option, cost, a sourced annual-attendance figure, and a
size bucket derived deterministically from that figure (massive / large / medium /
small, an objective proxy for prominence — e.g. RSNA ≈ 45,000 attendees = massive). It
normalizes that information into a typed schema, stores it in a
SQL database, exposes it through a boolean-searchable web table, and serves each
conference's deadlines and dates as a credential-free iCalendar (`.ics`) feed.
Discovery is seeded
across medicine (radiology and ~18 other specialties), genomics/bioinformatics,
and data science; the seed list (`SEED_CONFERENCES` in `config.py`) is the lever
for adding fields, and the standing refresh subcategories are derived from it.

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
    / `abstract_month` / `late_abstract_month` / `paper_month` (registration is
    free text — `registration` property, no derived month) and
    `size` / `attendance_display` / `format` / `subcategory` properties);
    `ConferenceSize` and `RemoteOption` enums; `CATEGORIES` and
    `SUBCATEGORY_TO_CATEGORY` (the top-level vocabulary + derivation map);
    `CONFERENCE_FORMATS` vocabulary; `size_for_attendance` (the size-bucketing rule),
    `categories_for_subcategories` (the category derivation), `normalize_subcategories`
    (the shared tag parser), and `normalize_formats` (the format-vocabulary parser).
    each deadline kind's time of day and time zone are structured per-series
    fields (`abstract_time` / `abstract_timezone`, likewise `late_abstract_*` and
    `paper_*`), from which the display text `deadline_time` is derived (see the
    design decision below)
  - `deadline_time.py` — the deadline-time vocabulary: `normalize_time` (any of
    24-hour / 12-hour / "noon" → canonical `HH:MM`), `normalize_timezone` (free
    text → a code in `TIMEZONES`, a `UTC±N` offset, or an IANA name), the display
    formatters (12- or 24-hour, identical times collapsed into one entry), and
    `parse_legacy_deadline_time` (the old free text → structured fields)
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
    "due" for re-discovery (6–24-month staleness window from the last edition's
    submission deadline, biweekly re-check via a `last_checked` column) so
    `daily_update.py --cadence due` targets only them; and the page-gated
    **watch** cadence (`run_watch`, the scheduled job): tiers series by deadline
    proximity, runs the agent-free page check, and re-researches only changed
    series via the targeted `discover.refresh_conferences`
  - `page_watch.py` — agent-free change detection: fetches a conference's link
    plus a few same-site dates pages and fingerprints the set of dates mentioned
  - `notify.py` — email summary after a discovery / daily refresh
  - `subscriptions.py` — per-conference update emails to site visitors: diffs
    each series' subscriber-facing fields against the last run's snapshot
    (`data/notify_state.json`) and emails that series' subscribers the changes
    plus its updated `.ics`, with signed unsubscribe links
  - `cli.py` — command-line entry point (`discover` / `add` /
    `delete` / `lookup`). A bare `discover` surveys every field
    (`database.discovery_subcategories`: table tags ∪ seed fields), one agent run
    per field; `--subcategory` / `--category` narrow the survey, while
    `--conference-name` / `--size` switch to re-checking matching stored series
    via `discover.refresh_conferences` (no new rows); `discover --options` lists
    the valid filter values and `lookup --columns name` the stored names. `_SCALAR_FIELDS` + `_COMPOSITE_FIELDS` is the single
    registry defining what `add` accepts; it generates the argparse flags, the
    `--csv`/`--json` column vocabulary, and the `add --fields` reference output, so a
    new field is added in one place and every input path picks it up. Entries
    are indexed by `conference_name` (see the name-index design decision): `add`
    fails if the name exists, `add --update` and `delete` (= `add --delete`) fail
    if it does not, and any conflict in a batch writes nothing.
    `--update --new-conference-name` renames a series (its id follows)
- `web/` — FastAPI app + static single-page table (`search.py` boolean-query
  language, `nl_query.py` optional natural-language → boolean-query translation
  via a local Ollama model, `app.py` REST API, `static/index.html`, `handler.py`
  Lambda entry point, `requirements.txt` for the Lambda build). For the
  credential-free static deployment, `static/search.js` and `static/calendar.js`
  reimplement `search.py` and `calendar_sync.py` in the browser (see the
  static-hosting design decision below)
- `scripts/` — runnable entry points (`build_table.py`, `daily_update.py`,
  `push_db.py`, `deploy.sh` one-command reconcile + deploy, `scheduled_discovery.sh`
  the daily cron job, `build_static.py` the static-site bundler,
  `notify_subscribers.py` the subscriber-email step of the cron job)
- `scripts/seo_pages.py` — prerenders crawlable per-conference (`/c/<id>/`, with
  `Event` JSON-LD) and per-field (`/field/<tag>/`) pages, `sitemap.xml`,
  `robots.txt`, and the home page's browse links; called by `build_static.py`
  (the table itself is JS-rendered, so crawlers need these). The payload's
  `generated` date is the "Last updated" shown in the header. Vercel Web
  Analytics (`/_vercel/insights/script.js`) must be enabled in the project dashboard.
- `web/vercel/` — the Vercel Functions behind the table's ✉️ subscribe button
  (`api/subscribe.js`, `confirm.js`, `unsubscribe.js`, `subscriptions.js`,
  shared `_lib.js`) and their `package.json`; `build_static.py` copies them into
  `dist/`. `api/propose.js` backs the "Add a conference" form
  (`web/static/add.html`, served at `/add/`, linked from the header and below
  the table): the form is generated from `data/add_fields.json`
  (`cli.add_field_schema`, the same vocabulary as `add --fields json`), and a
  submission is validated, committed as `submissions/<id>-<date>-<rand>.json`
  (an `add --json` record) on a new branch, and opened as a pull request;
  nothing reaches the table until a maintainer merges it and runs
  `conference-agent add --json` on the file
- `infra/` — AWS SAM deployment (`template.yaml`: CloudFront over an
  IAM-protected Lambda Function URL + RDS PostgreSQL in a VPC, with optional
  `DomainName`/`AcmCertificateArn` for a custom domain; `samconfig.toml`); built
  via the root `Makefile`. The serving Lambda injects Google Analytics when
  `GaMeasurementId` is set (AWS-only; the static site never carries it), and
  setting `EnableScheduledRefresh=true` provisions an EventBridge Scheduler +
  refresh Lambda (`web/refresh_handler.py`) + NAT gateway that replace the old
  GitHub Actions refresh cron. This AWS path is a private, on-demand demo kept
  torn down to $0 — see the gitignored `DEPLOY_AWS.md` run-book.
- `tests/` — offline unit tests (network/LLM tests are marked and excluded from CI)
- `data/` — generated tables / databases (gitignored, never committed)
- `.github/workflows/` — `ci.yml` only (lint + offline tests). The scheduled
  refresh workflows (`weekly_update`/`monthly_update`/`auto_check`) were replaced
  by EventBridge Scheduler in the AWS SAM stack (see `infra/template.yaml`)
- `DEPLOY.md` — deploy guide: the static Vercel path (live) and the legacy AWS stack

## Development Setup

Use the `conference_agent` conda environment for all work in this repo:

```bash
conda activate conference_agent
```

```bash
pip install -e .                   # core: lookup / add / delete (no discovery)
pip install -e ".[dev]"            # core + test tooling (includes web + discover)
pip install -e ".[discover]"       # discovery: anthropic (api backend) + fetch/parse helpers
pip install -e ".[web]"            # FastAPI web table + calendar feed
```

`pyproject.toml` is the authoritative source for dependencies. Add new
dependencies there rather than installing ad hoc.

### Credentials (not committed)

- `ANTHROPIC_API_KEY` — required only for the `api` discovery backend (and the
  AWS scheduled-refresh Lambda, which uses the `api` backend). The default
  `claude-code` backend uses the local Claude Code subscription and needs no key.

## Architecture / Key Design Decisions

- **One record per conference series, indexed by name.** The `Conference`
  schema keys on the full name and holds both the **prior** and **upcoming** editions
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
- **One canonical, sortable abstract deadline plus an explicit late one.** Some
  series publish two abstract deadlines for the same edition: a poster-only
  deadline after a talk-only main one (CSHL Biological Data Science — talks Aug
  28, posters Oct 1), or a late-breaking / late-poster round after the main call
  closes (ASHG, ISMB, RECOMB). A survey of 12 genomics/ML series found 1 of the
  former and 6 of the latter, so this is common enough to model but far from
  universal. Rather than overload one column with two dates (which would break
  sorting, the `abstract_month` derivation, date comparisons in the search, and
  the `.ics` event) or add a column per presentation type (which would serve only
  the oral/poster case and not late-breaking), there is one extra date field per
  edition: `*_late_abstract_deadline`, with `late_abstract_month` derived from it
  exactly as `abstract_month` is from the main deadline. `*_abstract_deadline`
  always holds the **earliest, primary** deadline — in every case surveyed the
  earliest is also the main submission route, so sorting on it never overstates
  the time remaining. The pair is threaded through the search
  (`late_abstract_due` / `late_abstract_month`), the table (two columns), the
  calendar (a fourth event, kind `late-abstract`), the discovery prompts, and
  the manual `add` paths.
- **Deadline time of day is structured per deadline kind, and the display text
  is derived.** What matters is the time zone ("23:59 AoE" vs "11:59 PM ET" is
  nearly a day apart), and a series keeps its convention year to year, so the
  time is per series, not per edition. Six nullable columns (not shown in the
  table or the CSV) are the source of truth: `abstract_time`, `late_abstract_time`,
  `paper_time` (always 24-hour `HH:MM`) and `abstract_timezone`,
  `late_abstract_timezone`, `paper_timezone` (a code from
  `deadline_time.TIMEZONES` — `AoE`, `UTC`, region codes such as `ET`/`CET`
  that follow daylight time, so EST/EDT/"Eastern Time" all store as `ET` — or a
  `UTC±N` offset or IANA name). Input may be 12- or 24-hour and any zone
  spelling; the model validators canonicalize it, so storage never carries a
  display preference and the browser never parses free text. The user-facing
  `deadline_time` is *derived* (like `size` / `category`; stored denormalized,
  never accepted as input except as a legacy shorthand that is parsed into the
  six fields): `"11:59 PM ET"` when every deadline the series has shares one
  time (identical times show once, not "abstract: … paper: …"), otherwise one
  `kind: time` line per deadline, omitting a dated deadline whose time was not
  published. Databases predating the columns are migrated on first open
  (`database.backfill_deadline_times`). The deadline columns stay pure dates so
  sorting, the derived months, the search's date comparisons, and the all-day
  calendar events are untouched. The table's "Deadline time" column
  (substring-searchable as `deadline_time:`) has a ⚙ box with a "Military time
  (24-hour)" checkbox (display only, remembered per browser; default 12-hour),
  and the calendar feed puts the applicable time in each deadline event's note
  (`calendar_sync.deadline_time_for`, mirrored in `calendar.js`).
  **Coloring is instant:** each colored date carries the instant its deadline
  passes (`calendar.js` `deadlineInstant`: the end of its stated minute in its
  zone; with no time the end of the day, and with no usable zone AoE, the
  latest day-end anywhere), and `index.html` re-judges the colors from a timer set
  for the next such instant (plus local midnight and tab re-focus), so a date
  goes from green to red the moment it passes with no re-render or redeploy.
  The zone table and the display grouping exist in both Python and JS;
  `tests/test_deadline_time.py` pins them together via Node.
- **Controlled vocabularies.** `ConferenceSize` (`massive`/`large`/`medium`/`small`) and
  `RemoteOption` (`in-person`/`virtual`/`hybrid`; an unknown option is NULL) are enums, not free
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
  buckets the `attendance` integer (≥10,000 → massive, ≥1,000 → large, ≥250 →
  medium, else small;
  blank when attendance is unknown — thresholds live in `models.py`). The stored `size` column is only ever written
  by that function, so it can never drift from the figure. Each attendance figure
  carries the year it describes and the source URL it came from (provenance kept
  internal — not exposed via the API/CSV); the table shows it as e.g.
  `45,000 (2025)`.
- **SQLAlchemy over raw SQL.** The same ORM runs against SQLite (local) or any
  SQLAlchemy backend with only a connection-string change.
- **Name index; the id is the name's slug.** Two series can share an acronym
  (ICML is also a lymphoma meeting), so the name is the key: `Conference.id` is
  `models.name_id(name)`, a lowercase hyphenated ASCII slug (case, accents,
  punctuation, and spacing do not distinguish names). The id is what calendar
  UIDs, stored email subscriptions, `data/notify_state.json`, and the `/c/<id>/`
  pages carry, so when an id changes (a rename via `database.rename_conference`,
  or the one-time move off upper-cased acronym ids done on open by
  `database._migrate_ids_to_names`) the former id is recorded in the
  `id_aliases` table. `resolve_ids` maps former ids forward: the subscriber
  step uses it for snapshot entries and Blob subscriptions stored under old ids
  (unsubscribe links keep naming the stored id), `merge_records` accepts a
  former `id`, and `build_static.py` writes `dist/vercel.json` with permanent
  redirects from each former `/c/<id>/` page. A rename does change that series'
  calendar UIDs, so a re-imported `.ics` adds new events.
- **Idempotent ingestion; discovery never renames.** Upserts match an existing
  row by name id (or a former id); failing that, by the same acronym with an
  overlapping subcategory, because the agent does not always reproduce a stored
  name verbatim (`database.match_row`). A same-acronym series in an unrelated
  field becomes its own row. The stored name is kept on every match; only an
  explicit rename changes it. The prompts ask the agent to reuse the listed
  names, and `discover.refresh_conferences` returns results under the targets'
  stored names. Per-seed curation in `config.py` (links, subcategories,
  formats) is keyed by seed acronym but looked up from the row's *name*
  (`config.seed_acronym_for_name`), so it never applies to a different series
  that shares the acronym; seed names must therefore match the stored names.
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
  "Subscribe (.ics)" feed URL that mirrors the active search. One search box
  serves both modes: the exact boolean search runs first, and when it matches
  nothing (or the text does not parse) the page falls back to
  `keywordSearch` in `web/static/search.js`, a forgiving, relevance-ranked
  keyword match (prefix/stem/typo tolerant, filler words dropped, rows matching
  more terms first). The fallback is not in the API; its Python port,
  `web.search.keyword_search`, serves `conference-agent lookup` (unique column
  values over the same search), so the boolean grammar, its Python parity, and
  the API are unchanged; `tests/test_keyword_search.py` covers it via Node and
  pins the Python port to it. An "✨ AI search" button next to Search (or Ctrl+Enter)
  sends the same box's text to the natural-language translator. Categorical
  column headers (category, subcategory, format, size, remote, the four month
  columns) carry an Excel-style ■ checkbox value filter; clicking a cell (or
  one tag in it) in those columns opens the same list beside it with that value
  pre-checked. The chosen values are
  browser-only state held apart from the search box and ANDed with it, so they
  survive an AI search and the boolean grammar (which has no exact-match
  operator for text) stays unchanged. A "Browse by field" bar above the search box (one
  card per category with rows, then the chosen category's subcategories as
  chips, grouped via the snapshot's `subcategory_categories` map) sets those
  same category / subcategory filters, and the page mirrors them and the
  search text in the URL (`/?category=<slug>`, `/?subcategory=machine-learning`,
  `&q=...`; back/forward step through selections), so a field's view is a
  shareable link. The `/field/<tag>/` pages link to these URLs.
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
- **Per-conference update emails (the one piece of server compute).** The ✉️
  subscribe button next to 📅 cal asks for an email address (remembered in
  `localStorage`, so later it is prefilled and Enter subscribes) and POSTs to the
  Vercel Function `/api/subscribe`. Subscriptions are double opt-in: the first
  time, a signed confirmation link (HMAC over email + id + expiry, keyed by
  `SUBSCRIBE_SECRET`) is emailed and nothing is stored until a person clicks
  Confirm on the linked page (GET only renders a button, because mail scanners
  prefetch links). Confirming writes `subs/<base64url(email)>/<id>` to a
  **private** Vercel Blob store and sets an HttpOnly `ca_verified` cookie, so
  later subscriptions for that address from the same browser apply without
  another email. Confirmation emails are capped at 25 per address per day. The
  sending half runs locally: after each cron refresh (and redeploy),
  `scripts/notify_subscribers.py` reads the list from the bearer-protected
  `/api/subscriptions`, and for each series whose watched fields
  (`subscriptions.WATCHED_FIELDS`) changed, emails its subscribers the changes
  with the series' `.ics` attached (same UIDs as the site, so re-importing
  updates events in place) and RFC 8058 one-click unsubscribe links. A series'
  snapshot advances only after its sends succeed, so failures retry next run.
  The tokens must match between `web/vercel/api/_lib.js` and
  `subscriptions.sign` (a test pins a JS-computed value). The functions run only
  on subscribe / confirm / unsubscribe, never for browsing or search.
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

  `scripts/deploy_static.sh` wraps exactly these two steps (activating the
  conda env if needed), so `bash scripts/deploy_static.sh` is the one-command
  deploy. The Vercel CLI must already be logged in, or `VERCEL_TOKEN` set.

  The live site is `https://conferenceagent.vercel.app`, served as static files
  directly from Vercel — no per-request compute and no AWS in the request path.
  `dist/` is linked (via `dist/.vercel/project.json`) to the Vercel project
  **conferenceagent**; deploy only from `dist/`. Cloudflare Pages
  (`npx wrangler pages deploy dist`) is an equivalent static host if ever needed.
  To refresh the live data, re-run `build_static.py` after a discovery run and
  redeploy — there is no database to push. The export omits **retired** series
  (`refresh.is_retired`: no future edition and the last one is more than
  `CHECK_WINDOW_MAX_MONTHS` old, i.e. past the point auto-checks stop); rows are
  never deleted, so a series reappears once discovery records a new edition.
  `--include-retired` exports everything.
- **Legacy AWS path (no longer the deploy target).** `scripts/deploy.sh` (push
  local DB → RDS; `DEPLOY_CODE=1` also `sam build` + `aws lambda
  update-function-code`) and the `deploy/vercel/` proxy (a rewrite to CloudFront)
  belong to the retired AWS stack documented in `DEPLOY.md`. Do **not** run them
  for a normal deploy — `deploy/vercel/` is linked to the same Vercel project, so
  deploying from it would revert the public URL to proxying AWS. Use them only
  when explicitly working on the AWS stack.
- **Automatic refresh.** A local cron entry (`0 2 * * *`) runs
  `scripts/scheduled_discovery.sh` daily. It runs `daily_update.py --cadence
  watch`, which page-checks series by tier (deadline within ±14 days: daily; a
  deadline or meeting within 30 days, or 6–24 months since the last edition:
  every 14 days) and calls the agent only for series whose pages' dates changed
  (or that are unreadable / past a 28-day backstop). It redeploys (via
  `scripts/deploy_static.sh`) only when the exported site data changed; the DB
  file itself changes every run from bookkeeping columns, so it is not the
  comparison. cron's minimal PATH lacks `~/.local/bin` (`claude`) and nvm
  (`vercel`); the script adds both, and sources email secrets (`SMTP_USER`,
  `SMTP_PASSWORD`, `SUBSCRIBE_SECRET`) from
  `~/.config/conference-agent/secrets.env` (outside the repo). After the
  redeploy it runs `scripts/notify_subscribers.py` (see the update-emails design
  decision). Logs: `data/logs/`.
- **Update-email setup (Vercel).** The functions need a private Blob store
  connected to the **conferenceagent** project and the env vars
  `SUBSCRIBE_SECRET` (same value as the local secrets file), `SMTP_USER`, and
  `SMTP_PASSWORD` (optionally `SMTP_HOST` / `SMTP_PORT`) in its Production
  environment.
- **Add-a-conference setup (Vercel).** `api/propose` needs `GITHUB_TOKEN` (a
  fine-grained token on the repository with Contents and Pull requests
  read/write) in the Production environment; `GITHUB_REPO` / `GITHUB_BASE`
  override the default `josephrich98/conference-agent` / `main`. It uses the
  same Blob store to rate-limit submissions (10 per client address and 100 in
  total per day).

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
