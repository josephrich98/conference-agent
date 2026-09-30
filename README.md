# conference_agent

<!-- Live site. Served as a static bundle directly on Vercel (no AWS in the
request path): `python scripts/build_static.py` then `cd dist && npx vercel
deploy --prod`. See DEPLOY.md. The legacy AWS CloudFront/Lambda stack is a
dynamic-backend alternative, not the deploy target. -->
https://conferenceagent.vercel.app

An AI agent that automatically compiles a table of major conferences. Includes
website links, color-coded prior and upcoming submission deadlines and dates, and 
daily checks for changed deadlines (see "Scheduled refresh"). 
Keeps it searchable in a web table, and exports each conference's deadlines and dates 
as a subscribable calendar feed (`.ics`). Discovery is seeded across medicine, 
genomics/bioinformatics, and data science; see `TAXONOMY.md`
for the field map and `SEED_CONFERENCES` in `config.py` to add more.

## Install

```bash
conda activate conference_agent
pip install -e ".[dev]"        # core + test tooling
pip install -e ".[discover]"   # discovery agent helpers
pip install -e ".[web]"        # FastAPI web table + calendar feed
```

## Usage

```bash
conference-agent discover --subcategory radiology --email   # find + store (+ email summary)
conference-agent list                                    # print the stored table
conference-agent serve                                   # launch the web table at :8000
```

## Add conferences manually

To enter or correct dates by hand — no API, no discovery agent — use
`conference-agent add`. Three interchangeable inputs share one field vocabulary:
flags for a single conference, `--csv` or `--json` for many.

Start with the field reference, which is generated from the same registry that
defines the flags, so it can never fall out of date:

```bash
conference-agent fields          # human-readable table
conference-agent fields --json   # machine-readable schema (for an agent)
```

Every name it lists works three ways — as a `--flag`, as a `--csv` header
column, and as a `--json` record key. Dates are ISO `YYYY-MM-DD`. The `Category`,
`Size` and month columns are *derived* on write and cannot be set by hand. By
default only the fields you supply are written, so an existing series keeps the
rest of its data:

```bash
# Add (or update) a single conference via flags.
conference-agent add \
  --conference "RSNA - Radiological Society of North America Annual Meeting" \
  --subcategory radiology "machine learning" \
  --format abstract poster oral \
  --location "Chicago, IL" \
  --attendance 39000 --attendance-year 2025 \
  --remote-option hybrid \
  --cost "\$1,095 (member, early-bird)" \
  --abstract-due 2026-05-06 \
  --conference-dates 2026-11-29 2026-12-03 \
  --url https://www.rsna.org/annual-meeting
```

## Scheduled refresh

A local cron job runs `scripts/scheduled_discovery.sh` every day at 2 AM. It
runs the **watch** cadence (`daily_update.py --cadence watch`), which avoids an
agent run unless something appears to have changed:

| Tier | Which series | Page check |
|---|---|---|
| daily | an upcoming submission deadline within 14 days before or after today | every day |
| soon | an upcoming deadline or the meeting's start within the next 30 days | every 14 days |
| stale | no future edition on record; the last edition's submission deadline (or start date) 6–24 months ago | every 14 days |

The page check needs no agent: it fetches the official link plus up to three
same-site "dates" / "deadlines" / "call for abstracts" pages and fingerprints the
set of dates they mention (`conference_agent/page_watch.py`). The agent then
re-researches a series, in a targeted run of up to 6 named series rather than a
whole field, only when:

- the fingerprint changed (an extension, a moved meeting, a new edition);
- the pages could not be read (blocked, rendered by JavaScript, no link) and the
  series has not been researched in 14 days; or
- 28 days have passed without research (a backstop, since a new edition is often
  announced on a new, year-specific site the stored link never shows).

At most 18 series are researched per run; the rest wait for the next day.
Targeted results are merged without clearing fields the agent did not re-find.
If the data the site shows changed, the job redeploys the static site
(`scripts/deploy_static.sh`). Logs go to `data/logs/`. Preview a run without
calling the agent or writing anything with:

```bash
python scripts/daily_update.py --cadence watch --dry-run
```

The windows and limits are constants in `conference_agent/config.py`. The
older per-field cadences remain available: `--cadence due` (fields holding a
series in the 6–24-month window), `weekly` (flagship fields), `monthly` (the
rest), and `all`. The AWS SAM stack can schedule those via EventBridge Scheduler
(`EnableScheduledRefresh=true`; see `DEPLOY_AWS.md`), but that stack is torn
down.

See `TAXONOMY.md` for the field map and cadence policy.

### Manual refresh (running outside the auto-check window)

If you want to refresh on demand (outside of the scheduled refresh)
— run `daily_update.py` yourself. By default these use the
`claude-code` backend (the local `claude` CLI / your subscription), so no
`ANTHROPIC_API_KEY` is required:

```bash
# Refresh a single field immediately, ignoring the staleness window entirely.
# --subcategory overrides the cadence selection, so the 6–24-month window and the
# 14-day re-check interval do NOT apply — it re-discovers that field right now.
python scripts/daily_update.py --subcategory genomics

# Refresh several fields at once.
python scripts/daily_update.py --subcategory genomics --subcategory radiology

# Refresh everything (every seeded field), also bypassing the due-window gating.
python scripts/daily_update.py --cadence all

# Run the same page-gated check the scheduled job runs.
python scripts/daily_update.py --cadence watch

# Refresh the fields holding a series in the 6-24-month window.
python scripts/daily_update.py --cadence due

# Add --no-email to skip the summary email; add --backend api to use the
# metered Anthropic API instead of the local claude CLI.
```

For a one-off discovery of a single field without the refresh wrapper:

```bash
conference-agent discover --subcategory genomics --email
```

## REST API (for agents and scripts)

The web layer is a read-only, credential-free REST API, so any AI agent or
script that can make an HTTP GET can pull the data directly — no MCP server or
custom integration required. Interactive docs are at
[`/docs`](https://conferenceagent.vercel.app/docs).

| Endpoint | Purpose |
| --- | --- |
| `GET /api/search?q=<query>&format=json` | Boolean search; returns the conference rows as JSON (`format=csv` for a CSV export). |
| `GET /api/fields` | Self-describing list of queryable fields, aliases, and controlled vocabularies — call this first to learn the query grammar at runtime. |
| `GET /api/calendar.ics?q=<query>` | The selected conferences as a subscribable iCalendar feed. |

The `q` parameter uses the same boolean query language as the web table (see
[Boolean search](#boolean-search) below); an empty `q` matches everything. An
agent that needs to construct valid queries should read `/api/fields` first,
since it returns the exact field names, aliases, and allowed values the parser
accepts.

## Boolean search

The web table accepts queries like:

- `(virtual OR hybrid) AND size:large`
- `subcategory:radiology NOT cost:*`
- `category:medicine AND size:large`
- `format:poster AND format:oral`
- `upcoming:>=2026-06-01`

Fields support `field:value`, `field:"quoted value"`, presence tests (`field:*`),
date comparisons (`>`, `>=`, `<`, `<=`, `=`), `AND`/`OR`/`NOT`, and parentheses.

### Plain-English search (optional, local LLM)

The "✨ Ask" box turns a plain-English request — *"big radiology
conferences between September and January"* — into the boolean query
above, then drops it into the search box (visible and editable) and runs it. The
translation runs on a **free, local [Ollama](https://ollama.com) model** — no API
key and no external network call — and the model's output is validated against
the real parser (with one repair round) before it is shown.

It is entirely optional: if no local model is running, the box reports that and
the manual boolean search keeps working. To enable it:

```bash
# one-time: install Ollama, then pull a small instruction model
ollama pull qwen2.5:1.5b     # the default; tiny and fast
ollama serve                 # if not already running as a service
```

`GET /api/translate?q=<plain English>` returns the compiled `{"query": ...}`.

The default `qwen2.5:1.5b` is the lightest option and handles direct requests
well, but it can stumble on multi-step phrasing — e.g. a wrap-around month range
like "September through January of any year." A larger model translates those
more reliably; point `CONFERENCE_NL_QUERY_MODEL` at one you have pulled:

```bash
CONFERENCE_NL_QUERY_MODEL=llama3.2:3b conference-agent serve   # or qwen2.5:7b
```

## Configuration

- `ANTHROPIC_API_KEY` — required only for the `api` discovery backend. The
  default `claude-code` backend uses the local `claude` CLI / your Claude Code
  subscription and needs no key (for unattended runs it uses
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` instead).
- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` and
  `CONFERENCE_NOTIFY_EMAIL` — optional, enable the summary email.
- `SUBSCRIBE_SECRET` (and the SMTP settings above) — enable the per-conference
  update emails that visitors request with the table's ✉️ notify button. The
  same secret must be set in the Vercel project, which also needs a private
  Blob store (see CLAUDE.md, "Update-email setup"). `CONFERENCE_SITE_URL`
  (default `https://conferenceagent.vercel.app`) sets the site the links point to.
- `CONFERENCE_DATABASE_URL` — optional, overrides the default SQLite location.
- `OLLAMA_BASE_URL` (default `http://localhost:11434`), `CONFERENCE_NL_QUERY_MODEL`
  (default `qwen2.5:1.5b`), and `CONFERENCE_NL_QUERY_TIMEOUT` — optional, configure
  the local model used for plain-English search.

## Deploy (static site on Vercel)

The live site is a **static bundle served directly on Vercel** — no per-request
compute and no AWS in the request path. Snapshot the catalog and publish:

```bash
python scripts/build_static.py                 # snapshot DB + UI into dist/
( cd dist && npx vercel deploy --prod --yes )  # publish to conferenceagent.vercel.app
```

Search, sort, CSV export, and per-row `.ics` download all run client-side over
the JSON snapshot. The only server code is the small set of Vercel Functions in
`web/vercel/api/` behind the ✉️ notify button (subscribe / confirm /
unsubscribe). Cloudflare Pages (`npx wrangler pages deploy dist`) is an
equivalent static host. See [DEPLOY.md](DEPLOY.md).

> **Legacy AWS path (no longer the deploy target).** The web table can also run
> as a FastAPI app on AWS Lambda + RDS PostgreSQL via the SAM template in
> `infra/` (`pip install -e ".[web,deploy]"; cd infra && sam build && sam deploy
> --guided`). It is kept as a dynamic-backend alternative — see DEPLOY.md.

To test locally:

```bash
python -m conference_agent.cli serve
```
*then follow URL*

## License

MIT
