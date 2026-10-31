# AGENTS.md

Guidance for AI coding agents (Codex, Claude Code, Cursor, and others) working in
this repository. `CLAUDE.md` holds the full architecture and design rationale;
read it before making non-trivial changes. This file collects the rules and
commands every agent needs.

## What this project is

`conference_agent` compiles a curated table of academic and professional
conferences: one record per conference series (prior + upcoming editions,
deadlines, dates, attendance, subcategory tags). An LLM discovery agent fills a
SQL database, which is served as a boolean-searchable web table and as
iCalendar (`.ics`) feeds. The live site is a static bundle on Vercel
(`https://conferenceagent.vercel.app`).

## Answering questions about conferences (for agents using the data)

If a user asks for a conference's deadline, dates, location, cost, or similar, read
it from the published data instead of searching the web. It is static JSON with no
key or rate-limited API:

- `https://conferenceagent.vercel.app/c/<id>/data.json` is one conference's full row.
  `<id>` is the name slug (e.g. `american-urological-association-annual-meeting`).
- `https://conferenceagent.vercel.app/data/conferences.json` is the whole catalog
  (`conferences` list). Find the `id` by matching `acronym` or `name`; acronyms are
  not unique, so check `name` and `subcategory`.
- Example, the AUA abstract deadline time: take `upcoming_abstract_deadline` (date),
  `abstract_time` (24-hour `HH:MM`), and `abstract_timezone` (`ET`, `AoE`, ...) from
  the row. `deadline_time` is the display text. Deadline fields come in
  `upcoming_*` and `prior_*` pairs; prefer `upcoming_*` and say when only `prior_*`
  exists.
- State the date the data was generated (`generated` in the catalog) and link the
  row's `url` so the user can confirm. Do not present a deadline as certain.

## Layout

- `conference_agent/` — core package: `models.py` (pydantic schema, controlled
  vocabularies, derived fields), `config.py` (constants, model id, refresh schedule),
  `discover.py` (LLM discovery), `database.py` (SQLAlchemy ORM + idempotent
  upserts), `calendar_sync.py` (`.ics` builder), `refresh.py` / `page_watch.py`
  (refresh cadence and agent-free change detection), `subscriptions.py` /
  `notify.py` (email), `cli.py` (`conference-agent` entry point),
  `deadline_time.py` (deadline time/zone vocabulary)
- `conference_agent/web/` — FastAPI app, boolean query language (`search.py`), and the static
  single-page UI in `conference_agent/web/static/` (with JS ports `search.js`, `calendar.js`,
  `nl_query.js`); `conference_agent/web/vercel/` holds the subscribe/confirm/unsubscribe functions
- `scripts/` — build, refresh, deploy, and notification entry points
- `infra/`, `deploy/` — legacy AWS stack (not the deploy target; see below)
- `tests/` — offline unit tests
- `data/`, `dist/`, `build/` — generated output; never commit

## Setup

```bash
conda activate conference_agent
pip install -e ".[dev]"
```

`pyproject.toml` is the authoritative dependency list. Add dependencies there;
do not install ad hoc.

## Checks (run before finishing)

```bash
ruff check conference_agent web scripts tests
pytest -q -m "not network and not llm"
```

These match CI (`.github/workflows/ci.yml`). Mark any test that touches the
network with `@pytest.mark.network` and any test that calls the Anthropic API
with `@pytest.mark.llm`, so CI stays hermetic. Some tests run JavaScript through
`node` and skip when it is absent.

## Rules that are easy to break

- **Derived fields are never hand-set.** `size`, the `*_month` columns, and
  `deadline_time` are computed (`size_for_attendance`, the deadline-time
  formatters). Change the inputs or the derivation, not the stored value.
  (`category` is an input, set independently of `subcategory`.)
- **Python and JavaScript must stay in parity.** `conference_agent/web/search.py` ↔
  `conference_agent/web/static/search.js`, `calendar_sync.py` ↔ `conference_agent/web/static/calendar.js`,
  `deadline_time.py` ↔ the zone table in `calendar.js`, `conference_agent.web.search.keyword_search`
  ↔ `keywordSearch`, and `subscriptions.sign` ↔ `conference_agent/web/vercel/api/_lib.js`. When you
  change one side, change the other; parity tests in `tests/` pin them together.
- **Series are keyed by name.** `Conference.id` is `models.name_id(name)`.
  Discovery never renames a row; renames go through `database.rename_conference`,
  which records the former id in `id_aliases`. Calendar UIDs are deterministic
  from the id and event kind; do not change that derivation.
- **CLI fields are registered once.** `_SCALAR_FIELDS` + `_COMPOSITE_FIELDS` in
  `cli.py` generate the `add` flags and the CSV/JSON column vocabulary. Add new
  fields there.
- **Anthropic model ids live in `config.py`.** Do not hard-code them elsewhere.

## Deployment

"Deploy" means the static bundle to Vercel:

```bash
bash scripts/deploy_static.sh   # = build_static.py, then vercel deploy --prod from dist/
```

Do **not** run `scripts/deploy.sh` or deploy from `deploy/vercel/` for a normal
deploy; they belong to the retired AWS stack, and the latter would repoint the
public URL. Do not deploy, push, or send email unless the user asks.

## Conventions

- Never commit secrets (`ANTHROPIC_API_KEY`, SMTP credentials,
  `SUBSCRIBE_SECRET`) or generated databases (`*.db`, `*.sqlite`, `*.sql`).
- American English, professional tone, no overstated claims.
- Match the surrounding code's style, naming, and comment density.
