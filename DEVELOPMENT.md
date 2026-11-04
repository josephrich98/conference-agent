# Development

This guide covers maintaining the conference database: editing it by hand,
running the discovery agent, and the scheduled updates that keep the live site
current. For querying the data, see [README.md](README.md).

## Install

```bash
git clone https://github.com/josephrich98/conference-agent
cd conference-agent
pip install -e ".[dev]"
```

`add`, `delete`, and `discover` are available only in a development install,
that is, when the package runs from this checkout (`pip install -e .`). A
regular `pip install conference-agent` provides just `lookup` and `info`. The
editable base install is enough to add or delete entries. To run the discovery
agent, install the `discover` extra (`pip install -e ".[dev,discover]"`), included
in `dev`.

## The local database

`add`, `delete`, and `discover` write to the local database (`--db`, default
`data/conferences.db`). That database is the source of the live site:
`scripts/build_static.py` exports it and `scripts/deploy_static.sh` publishes
it (see [DEPLOY.md](DEPLOY.md)), so a local change reaches the site on the next
deploy. `lookup` and `info` read the live site by default; pass `--local` to
search the local database, or `--db URL` to name another one.

Each entry is indexed by its full name (the `name` column, given to `add`,
`delete`, and `discover` as `--conference-name`).

## Commands

### conference-agent add

View all fields:

```bash
conference-agent add --fields
```

Add a new conference (will fail if the conference name already exists):

```bash
conference-agent add \
  --conference-name "Radiological Society of North America Annual Meeting" \
  --conference-acronym "RSNA" \
  --subcategory radiology \
  --format abstract poster oral \
  --location "Chicago, IL" \
  --attendance 39000 --attendance-year 2025 \
  --remote-option hybrid \
  --cost "\$1,095 (member, early-bird)" \
  --abstract-due 2026-05-06 \
  --conference-dates 2026-11-29 2026-12-03 \
  --url https://www.rsna.org/annual-meeting
```

The `--abstract-guidelines` and `--paper-guidelines` fields are free text: a
paragraph, or one requirement per line starting with `- ` (shown as a bulleted
list on the website):

```bash
conference-agent add --update \
  --conference-name "Radiological Society of North America Annual Meeting" \
  --abstract-guidelines $'- 2,200 characters maximum, excluding title and authors\n- Structured: Purpose, Methods, Results, Conclusion\n- One figure or table allowed'
```

The `--registration` field holds the upcoming edition's registration dates, one
`Label: date` item per published date (newline- or `;`-separated, any date
spelling, stored as `YYYY-MM-DD`): `Opens`, `Early bird deadline`, `Final
deadline`, `Refund deadline`, and a `Notes` item only for what the dates cannot
say (e.g. registration by lottery):

```bash
conference-agent add --update \
  --conference-name "Radiological Society of North America Annual Meeting" \
  --registration "Opens: Jul 23, 2026; Early bird deadline: Oct 2, 2026; Refund deadline: Nov 6, 2026"
```

Update an existing conference (will fail if the conference name does not exist):

```bash
conference-agent add --update \
  --conference-name "Radiological Society of North America Annual Meeting" \
  ...
```

Update an existing conference, including changing the conference-name field (will fail if the conference name does not exist):

```bash
conference-agent add --update \
  --conference-name "Radiological Society of North America Annual Meeting" \
  --new-conference-name "RSNA Annual Meeting" \
  ...
```

### conference-agent delete

Delete an existing conference (will fail if the conference name does not exist):

```bash
conference-agent delete \
  --conference-name "Radiological Society of North America Annual Meeting"
```

### conference-agent discover

Update the local database with Claude Code. This needs the `discover` extra and
either the Claude Code CLI (`claude`, the default backend) or an
`ANTHROPIC_API_KEY` for `--backend api`.

For a single conference:

```bash
conference-agent discover \
  --conference-name "Radiological Society of North America Annual Meeting"
```

For all conferences in a category:

```bash
conference-agent discover \
  --category "artificial intelligence"
```

For all conferences in multiple categories:

```bash
conference-agent discover \
  --category "artificial intelligence" \
  --category mathematics
```

For all conferences in a subcategory:

```bash
conference-agent discover \
  --subcategory radiology
```

For all conferences in a size:

```bash
conference-agent discover \
  --size massive
```

To update airports specifically:

```bash
conference-agent discover \
  --airports
```

To research the abstract and paper guidelines of conferences with a submission
deadline still ahead (those never researched, those with none found 14+ days
ago, and those researched 120+ days ago; at most 15 per run):

```bash
conference-agent discover \
  --guidelines
```

`--guidelines all` re-researches every conference with a deadline ahead, and the
filters above (`--conference-name`, `--category`, ...) narrow the selection.

To research the upcoming registration dates of conferences with a date still
ahead (those never researched, those with none or not yet in the line format
14+ days ago, and those researched 45+ days ago; at most 15 per run):

```bash
conference-agent discover \
  --registration
```

`--registration all` re-researches every conference with a date ahead.

Using the Claude API rather than a Claude Code subscription:

```bash
conference-agent discover \
  --conference-name "Radiological Society of North America Annual Meeting" \
  --backend api
```

## Local to website database conversion

- *underscores replaced with spaces, and capitalization applied as appropriate*
- acronym + name + url --> conference ([ACRONYM](URL) — NAME)
- upcoming_* + prior_* --> one column each, showing the upcoming value and falling back to the prior one
- upcoming_start_date + upcoming_end_date --> conference dates (START – END)
- abstract_month, late_abstract_month, paper_month, conference_month (all derived from date columns) --> month names (1 --> January)
- attendance + attendance_year --> attendance (45,000 (2025))
- abstract_time/timezone + late_abstract_time/timezone + paper_time/timezone --> deadline time (11:59 PM ET; 12- or 24-hour)
- abstract_guidelines, paper_guidelines --> own columns (after deadline time); lines starting with "- " are shown as a bulleted list
- upcoming_registration + prior_registration --> registration (left of conference month), one line per item, with the deadline dates colored like the other dates
- abstract_deadline_extension + late_abstract_deadline_extension + paper_deadline_extension --> no column of their own; embedded as a ⏩ button after the matching deadline date, listing the past extensions (original → extended) on hover, tap, or click
- added calendar (📅: subscribe to the conference's feed, or download a one-time .ics) and email (✉️: update emails, plus a reminder one week before each deadline) columns
- subscribable calendar feeds: `/c/<id>/calendar.ics` per conference, `/field/<tag>/calendar.ics` per field, and `/calendar.ics` for everything
- hidden: id, notes, attendance_source, last_checked, watch_*, guidelines_checked, registration_checked (internal bookkeeping)
- retired series (no future edition, last one over 24 months old) are left out of the website

## Scheduled updates

A local cron job runs `scripts/scheduled_discovery.sh` every day at 2 AM PST. It
runs the **watch** cadence (`daily_update.py --cadence watch`), which avoids an
agent run unless something appears to have changed:

| Tier | Which series | Page check |
|---|---|---|
| daily | an upcoming submission deadline within 14 days before or after today | every day |
| soon | an upcoming deadline or the meeting's start within the next 30 days | every 14 days |
| stale | no future edition on record; the last edition's submission deadline (or start date) 6–24 months ago | every 14 days |
| recent | no future edition on record; the last edition's submission deadline (or start date) under 6 months ago | every 14 days |

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
Once a meeting is over (and no deadline of that edition is still ahead), its
dates move from the upcoming columns to the prior ones; an edition recorded in
both is merged into one.

Each run also:

- adds "Add or edit a conference" submissions merged into `origin/main`
  (`scripts/ingest_submissions.py`; processed files are recorded in
  `data/ingested_submissions.json`, and a failed one is not retried);
- looks up the airports near any new or changed location
  (`conference-agent discover --airports`);
- researches the abstract and paper guidelines of conferences with a
  submission deadline ahead that are due (`conference-agent discover
  --guidelines`, using Sonnet): never researched, none found 14+ days ago, or
  last researched 120+ days ago, at most 15 per run, soonest deadline first;
- researches the upcoming registration dates of conferences with a date ahead
  that are due (`conference-agent discover --registration`, using Sonnet):
  never researched, none or not in the line format 14+ days ago, or last
  researched 45+ days ago, at most 15 per run, soonest meeting first;
- redeploys the static site (`scripts/deploy_static.sh`) if the data the site
  shows changed;
- emails subscribers about changed conferences, and once when a subscribed
  conference's deadline is a week away (`scripts/notify_subscribers.py`); and
- emails the end of the log to `CONFERENCE_NOTIFY_EMAIL` if any step failed
  (`scripts/alert_failure.py`).

Logs go to `data/logs/`. Preview a run without
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
