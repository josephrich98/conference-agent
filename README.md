# conference_agent

https://mmekgpcwq2fnenb2ifnvhk4sca0ugikt.lambda-url.us-east-1.on.aws

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
conference-agent discover --category radiology --email   # find + store (+ email summary)
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
  --category radiology "machine learning" \
  --location "Chicago, IL" \
  --reputation big \
  --remote-option hybrid \
  --cost "\$1,095 (member, early-bird)" \
  --abstract-due 2026-05-06 \
  --conference-dates 2026-11-29 2026-12-03 \
  --url https://www.rsna.org/annual-meeting
```

`--conference` takes the table's first column verbatim — `ACRONYM - Full Name`. A
bare acronym (`--conference RSNA`) updates an existing row in place, so you can
correct a single date with nothing else. `--category` accepts several
space-separated tags; introducing a tag no existing conference uses prints a
warning (a typo guard) but still adds it. If the series already exists the command
updates only the fields you pass — add `--overwrite` to replace the whole row
instead, clearing anything you omit (so it then needs at least the acronym, name,
and category). The added row behaves exactly like a discovered one: its website
becomes the conference-name hyperlink and it gets the per-row "📅 cal" button plus
inclusion in the subscribable calendar feed.

To load several at once, point `--csv` at a file whose header columns are the same
column names as the flags — `conference`, `category`, `location`, `reputation`,
`remote_option`, `cost`, `abstract_due`, `paper_due`, `conference_dates`, `url` —
with one conference per row. `conference_dates` is a single cell holding the start
and (optional) end date separated by a space, and a multi-tag `category` is quoted
so its comma stays inside one cell. The raw stored field names (`acronym`, `name`,
`upcoming_start_date`, …) are also accepted, so the web table's "Export CSV"
re-imports unchanged.

```bash
conference-agent add --csv my_conferences.csv
```

```csv
conference,category,location,reputation,remote_option,abstract_due,conference_dates,url
RSNA - Radiological Society of North America,radiology,"Chicago, IL",big,hybrid,2026-05-06,2026-11-29 2026-12-03,https://www.rsna.org/annual-meeting
SPR - Society for Pediatric Radiology,"radiology, pediatrics","Austin, TX",medium,in-person,2025-12-01,2026-05-12 2026-05-16,https://www.pedrad.org
MICCAI - Medical Image Computing & Computer Assisted Intervention,"radiology, machine learning","Daejeon, South Korea",big,hybrid,2026-03-05,2026-09-23 2026-09-27,https://miccai.org
```

## Scheduled refresh

Three workflows re-run discovery on a schedule (crons commented out by default;
all are manually dispatchable). `weekly_update.yml` refreshes the flagship,
fast-moving fields (`config.WEEKLY_CATEGORIES`) via
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
# --category overrides the cadence selection, so the 6–12-month window and the
# 14-day re-check interval do NOT apply — it re-discovers that field right now.
python scripts/daily_update.py --category genomics

# Refresh several fields at once.
python scripts/daily_update.py --category genomics --category radiology

# Refresh everything (every seeded field), also bypassing the due-window gating.
python scripts/daily_update.py --cadence all

# Run the same auto-check the scheduled job runs (only series currently "due").
python scripts/daily_update.py --cadence due

# Add --no-email to skip the summary email; add --backend api to use the
# metered Anthropic API instead of the local claude CLI.
```

For a one-off discovery of a single field without the refresh wrapper:

```bash
conference-agent discover --category genomics --email
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

- `(virtual OR hybrid) AND reputation:big`
- `category:radiology NOT cost:*`
- `upcoming:>=2026-06-01`

Fields support `field:value`, `field:"quoted value"`, presence tests (`field:*`),
date comparisons (`>`, `>=`, `<`, `<=`, `=`), `AND`/`OR`/`NOT`, and parentheses.

## Configuration

- `ANTHROPIC_API_KEY` — required only for the `api` discovery backend. The
  default `claude-code` backend uses the local `claude` CLI / your Claude Code
  subscription and needs no key (for unattended runs it uses
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` instead).
- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` and
  `CONFERENCE_NOTIFY_EMAIL` — optional, enable the summary email.
- `CONFERENCE_DATABASE_URL` — optional, overrides the default SQLite location.

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
python -m conference_agent.cli serve
*follow URL*

## License

MIT
