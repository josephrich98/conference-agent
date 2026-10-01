# conference_agent

Website: https://conferenceagent.vercel.app
Database: data/conferences.db

Conference database for artificial intelligence, medicine, and genomics. Keep up to date with due dates with calendar events and email reminders. 

## Install

```bash
git clone https://github.com/josephrich98/conference-agent
cd conference-agent
pip install .
```

The base install is enough to query the database and add or delete entries
(`lookup`, `add`, `delete`). To run the discovery agent, install the `discover`
extra:

```bash
pip install .[discover]
```

## Usage

```bash
conference-agent lookup    # query the database
conference-agent add       # add/update a conference manually
conference-agent delete    # delete a conference manually
conference-agent discover  # run the discovery agent to update conference information
```

Each entry is indexed by the conference_name column.

### conference-agent lookup

Query the database for conferences:

```bash
conference-agent lookup --query "subcategory:radiology AND size:massive"
```

Query the database for conferences, only returning specific columns:

```bash
conference-agent lookup --query "subcategory:radiology AND size:massive" \
  --columns name acronym upcoming_start_date location
```

View the entire database:

```bash
conference-agent lookup
```

View all unique values of a column:

```bash
conference-agent lookup --columns conference_name
```


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

Update the database with Claude code.

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

Using Claude API rather than Claude code subscription:

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
- added calendar (📅: subscribe to the conference's feed, or download a one-time .ics) and email (✉️: update emails, plus a reminder one week before each deadline) columns
- subscribable calendar feeds: `/c/<id>/calendar.ics` per conference, `/field/<tag>/calendar.ics` per field, and `/calendar.ics` for everything
- hidden: id, notes, attendance_source, last_checked, watch_* (internal bookkeeping)
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
