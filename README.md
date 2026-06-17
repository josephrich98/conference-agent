# conference_agent

https://mmekgpcwq2fnenb2ifnvhk4sca0ugikt.lambda-url.us-east-1.on.aws

An AI agent that automatically compiles a table of major conferences — with
prior and upcoming submission deadlines and dates, category, remote-attendance
option, cost, official link, and a reputability tier — keeps it searchable in a
web table, and exports each conference's deadlines and dates as a subscribable
calendar feed (`.ics`). Discovery is seeded across medicine (radiology and ~18
other specialties), genomics/bioinformatics, and data science; see `TAXONOMY.md`
for the field map and `SEED_CONFERENCES` in `config.py` to add more.

> **Status:** core pipeline implemented (schema, database, discovery agent,
> calendar feed, email notifier, and web table interface).

## What it tracks

One record per conference *series*, holding both its prior and upcoming editions:

| Field | Example |
| --- | --- |
| Name / acronym | Radiological Society of North America / `RSNA` |
| Category | `radiology` |
| Prior abstract / paper deadline | 2025-04-08 / 2025-05-13 |
| Prior conference dates | 2025-11-30 – 2025-12-04 |
| Upcoming abstract / paper deadline | 2026-04-08 / 2026-05-12 |
| Upcoming conference dates | 2026-11-29 – 2026-12-03 |
| Website | https://www.rsna.org/annual-meeting |
| Remote option | `in-person` / `virtual` / `hybrid` |
| Cost | $1,095 (member, early-bird) |
| Reputation | `big` / `medium` / `small` |

Reputation and remote option are controlled values (enums), so the table and
queries can filter and color consistently.

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

## Boolean search

The web table accepts queries like:

- `(virtual OR hybrid) AND reputation:big`
- `category:radiology NOT cost:*`
- `upcoming:>=2026-06-01`

Fields support `field:value`, `field:"quoted value"`, presence tests (`field:*`),
date comparisons (`>`, `>=`, `<`, `<=`, `=`), `AND`/`OR`/`NOT`, and parentheses.

## Configuration

- `ANTHROPIC_API_KEY` — required for the discovery agent.
- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` and
  `CONFERENCE_NOTIFY_EMAIL` — optional, enable the summary email.
- `CONFERENCE_DATABASE_URL` — optional, overrides the default SQLite location.

## Scheduled refresh

Two workflows re-run discovery on a schedule (crons commented out by default;
both are manually dispatchable). `weekly_update.yml` refreshes the flagship,
fast-moving fields (`config.WEEKLY_CATEGORIES`) via
`daily_update.py --cadence weekly`; `monthly_update.yml` refreshes every other
field via `--cadence monthly`. Both use the secrets above and upload the SQLite
database as an artifact when no persistent `CONFERENCE_DATABASE_URL` is
configured. See `TAXONOMY.md` for the field map and cadence policy.

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
