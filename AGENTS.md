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

## Layout

- `conference_agent/` — core package: `models.py` (pydantic schema, controlled
  vocabularies, derived fields), `config.py` (constants, seed list, model id),
  `discover.py` (LLM discovery), `database.py` (SQLAlchemy ORM + idempotent
  upserts), `calendar_sync.py` (`.ics` builder), `refresh.py` / `page_watch.py`
  (refresh cadence and agent-free change detection), `subscriptions.py` /
  `notify.py` (email), `cli.py` (`conference-agent` entry point),
  `deadline_time.py` (deadline time/zone vocabulary)
- `web/` — FastAPI app, boolean query language (`search.py`), and the static
  single-page UI in `web/static/` (with JS ports `search.js`, `calendar.js`,
  `nl_query.js`); `web/vercel/` holds the subscribe/confirm/unsubscribe functions
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
- **Python and JavaScript must stay in parity.** `web/search.py` ↔
  `web/static/search.js`, `calendar_sync.py` ↔ `web/static/calendar.js`,
  `deadline_time.py` ↔ the zone table in `calendar.js`, `web.search.keyword_search`
  ↔ `keywordSearch`, and `subscriptions.sign` ↔ `web/vercel/api/_lib.js`. When you
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
