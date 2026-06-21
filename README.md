# conference_agent

<!-- Live site. Served on AWS through CloudFront (an IAM-locked Lambda Function
URL behind Origin Access Control). A custom domain drops in via the
DomainName/AcmCertificateArn parameters — see DEPLOY.md. An optional Vercel proxy
(deploy/vercel) can front this with a free conferenceagent.vercel.app name. -->
https://dvkzefjrppdt8.cloudfront.net

An AI agent that automatically compiles a table of major conferences. Includes
website links, color-coded prior and upcoming submission deadlines and dates, and 
biweekly updates (every two weeks). 
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
`conference-agent add`. The flags mirror the table columns (the only extra is
`--url`, the link behind the conference name); dates are ISO `YYYY-MM-DD`, and the
submission/conference month columns are derived from the dates automatically. By
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

The `Size` column (large / medium / small) is derived automatically from
`--attendance` — there is no size flag to set. A figure of 1,000+ is large,
100–999 medium, under 100 small; with no attendance, size is left blank. (The
thresholds live in `conference_agent/models.py`.)

`--conference` takes the table's first column verbatim — `ACRONYM - Full Name`.
A bare acronym (or the `ACRONYM - Full Name` form) that matches a row already in
the table counts as a match: the command shows that entry and asks you to confirm
before updating it, so a typo can't silently overwrite an existing series. Pass
`-y`/`--yes` to skip the prompt (required when running unattended). If the series
already exists, the command updates only the fields you pass — add `--overwrite`
to replace the whole row instead, clearing anything you omit.

To load several at once, point `--csv` at a file whose header columns are the same
column names as the flags — `conference`, `subcategory`, `format`, `location`,
`attendance`,
`attendance_year`, `attendance_source`, `remote_option`, `cost`, `abstract_due`,
`paper_due`, `conference_dates`, `url` — with one conference per row (the derived
`size` and `category` columns, as in a table export, are accepted but ignored).
`conference_dates` is a single cell holding the start
and (optional) end date separated by a space, and a multi-value `subcategory` or
`format` (any of abstract / paper / poster / oral) is quoted
so its comma stays inside one cell. The broad `category` is derived from the
subcategory automatically, so you never set it. The raw stored field names
(`acronym`, `name`, `upcoming_start_date`, …) are also accepted, so the web
table's "Export CSV" re-imports unchanged.

```bash
conference-agent add --csv my_conferences.csv
```

```csv
conference,subcategory,location,attendance,remote_option,abstract_due,conference_dates,url
RSNA - Radiological Society of North America,radiology,"Chicago, IL",45000,hybrid,2026-05-06,2026-11-29 2026-12-03,https://www.rsna.org/annual-meeting
SPR - Society for Pediatric Radiology,"radiology, pediatrics","Austin, TX",1200,in-person,2025-12-01,2026-05-12 2026-05-16,https://www.pedrad.org
MICCAI - Medical Image Computing & Computer Assisted Intervention,"radiology, machine learning","Daejeon, South Korea",3500,hybrid,2026-03-05,2026-09-23 2026-09-27,https://miccai.org
```

## Scheduled refresh

Three workflows re-run discovery on a schedule (crons commented out by default;
all are manually dispatchable). `weekly_update.yml` refreshes the flagship,
fast-moving fields (`config.WEEKLY_SUBCATEGORIES`) via
`daily_update.py --cadence weekly`; `monthly_update.yml` refreshes every other
field via `--cadence monthly`. Both use the secrets above and upload the SQLite
database as an artifact when no persistent `CONFERENCE_DATABASE_URL` is
configured.

`auto_check.yml` is the targeted "auto-check": its (commented-out) cron runs
daily, but `--cadence due` only spends discovery calls on series whose next
edition is plausibly about to be announced — each is re-checked at most every
`RECHECK_INTERVAL_DAYS` (14) days, so the effective refresh per conference is
roughly every two weeks. It runs the `claude-code` backend (the local `claude`
CLI on a Claude Code subscription) rather than the metered API, so instead of
`ANTHROPIC_API_KEY` it needs `CLAUDE_CODE_OAUTH_TOKEN` (generate it locally with
`claude setup-token` and store it as a repo secret). Because the two-week
interval is gated by a `last_checked` column, this job needs a persistent
`CONFERENCE_DATABASE_URL` to work across runs.

See `TAXONOMY.md` for the field map and cadence policy.

### Manual refresh (running outside the auto-check window)

If you want to refresh on demand (outside of the biweekly GitHub Actions) 
— run `daily_update.py` yourself. By default these use the
`claude-code` backend (the local `claude` CLI / your subscription), so no
`ANTHROPIC_API_KEY` is required:

```bash
# Refresh a single field immediately, ignoring the staleness window entirely.
# --subcategory overrides the cadence selection, so the 6–12-month window and the
# 14-day re-check interval do NOT apply — it re-discovers that field right now.
python scripts/daily_update.py --subcategory genomics

# Refresh several fields at once.
python scripts/daily_update.py --subcategory genomics --subcategory radiology

# Refresh everything (every seeded field), also bypassing the due-window gating.
python scripts/daily_update.py --cadence all

# Run the same auto-check the scheduled job runs (only series currently "due").
python scripts/daily_update.py --cadence due

# Add --no-email to skip the summary email; add --backend api to use the
# metered Anthropic API instead of the local claude CLI.
```

For a one-off discovery of a single field without the refresh wrapper:

```bash
conference-agent discover --subcategory genomics --email
```

## How it works

1. **Discover** — `discover.py` runs an Anthropic web-search loop (research) then
   a structured-output call (extraction) to produce typed `Conference` records.
   A seed list of well-known conferences (`SEED_CONFERENCES` in `config.py`)
   bootstraps the search; the reference pages those seeds were compiled from are
   recorded alongside it in `SEED_CONFERENCE_SOURCES`.
2. **Store** — records are upserted into a SQL database, idempotent on the
   acronym, so re-running discovery rolls a newly announced edition into the
   "upcoming" columns instead of duplicating the row.
3. **Search** — a web table (`conference-agent serve`) supports a boolean query
   language, a "Subscribe (.ics)" calendar feed, and a per-row "📅 cal" button
   that downloads that conference as a calendar file.
4. **Calendar** — each conference's upcoming abstract deadline, paper deadline,
   and conference dates are served as a credential-free iCalendar feed
   (`GET /api/calendar.ics`) that mirrors the active search. A user subscribes
   from any calendar app (Google "Add by URL", Apple/Outlook "Add from URL") or
   downloads a one-off `.ics` — no sign-in, no Google account. Each event carries
   a stable id, so re-fetching updates events in place rather than duplicating,
   plus reminders four weeks, one week, and one day ahead.
5. **Notify** — an optional email summarizes a discovery / daily refresh.

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
- `CONFERENCE_DATABASE_URL` — optional, overrides the default SQLite location.
- `OLLAMA_BASE_URL` (default `http://localhost:11434`), `CONFERENCE_NL_QUERY_MODEL`
  (default `qwen2.5:1.5b`), and `CONFERENCE_NL_QUERY_TIMEOUT` — optional, configure
  the local model used for plain-English search.

## Deploy to AWS (FastAPI + Lambda + PostgreSQL)

The web table runs as a FastAPI app on AWS Lambda (public Function URL) backed by
RDS PostgreSQL, provisioned by the SAM template in `infra/`. Because the code is
plain SQLAlchemy, only the connection string changes from local SQLite. The
Lambda uses the pure-Python `pg8000` driver, so `sam build` needs no Docker.

```bash
pip install -e ".[web,deploy]"   # mangum + pg8000
cd infra && sam build && sam deploy --guided
```

See [DEPLOY.md](DEPLOY.md) for the full walkthrough (VPC/subnets, loading data
into RDS, and the scheduled-refresh wiring).

To test locally:

```bash
python -m conference_agent.cli serve
```
*then follow URL*

## License

MIT
