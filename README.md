# conference_agent

Website: https://conferenceagent.vercel.app
Database: data/conferences.db

Conference database for artificial intelligence, medicine, and genomics. Keep up to date with due dates with calendar events and email reminders. 

## Install

```bash
pip install git+https://github.com/josephrich98/conference-agent
```

The base install is enough to query the database and add or delete entries
(`lookup`, `add`, `delete`). To run the discovery agent, install the `discover`
extra:

```bash
pip install "conference_agent[discover] @ git+https://github.com/josephrich98/conference-agent"
```

It adds the Anthropic SDK (used by the `api` backend) and the page-fetching
helpers. The default `claude-code` backend also requires the `claude` CLI.

## Usage

```bash
conference-agent lookup    # query the database
conference-agent discover  # run the discovery agent to update conference information
conference-agent add       # add/update a conference manually
conference-agent delete    # delete a conference manually
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
  --columns conference_name acronym conference_dates location
```

View the entire database

```bash
conference-agent lookup
```

View all unique values of a column:

```bash
conference-agent lookup --columns conference_name
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
conference-agent add --delete \
  --conference-name "Radiological Society of North America Annual Meeting" \
  ...
```

### conference-agent lookup

List the unique values of one or more columns (alphabetical by default). With
no arguments, it prints every value of every column across all conferences:

```bash
conference-agent lookup                         # all values of every column
conference-agent lookup --columns subcategory   # all values in one column
```

Add `--query` to restrict the values to matching conferences. It uses the same
search as the website: the boolean query first, then a forgiving keyword match
when that finds nothing.

```bash
conference-agent lookup --columns acronym conference_month \
  --query "subcategory:radiology AND size:massive"
```

`--sort` takes `alphabetical` (default), `reversealphabetical`, `increasing`, or
`decreasing` (the last two order numbers, dates, and size by value).

## Scheduled updates

A local cron job runs `scripts/scheduled_discovery.sh` every day at 2 AM PST. It
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

## Data access (for agents and scripts)

The live site is static, so the data is available as one credential-free JSON
file that any agent or script can fetch with an HTTP GET:

```bash
curl https://conferenceagent.vercel.app/data/conferences.json
```

The file holds `generated` (the snapshot date), `columns` (the table's column
order), `fields` (the queryable fields, aliases, and controlled vocabularies),
and `conferences` (one object per conference series). Filter it client-side.

For server-side boolean search, CSV export, and a subscribable `.ics` feed, run
the FastAPI backend locally with `uvicorn web.app:app` (requires
`pip install -e ".[web]"`). Its endpoints are documented in
[`DEPLOY.md`](DEPLOY.md#rest-api-fastapi-backend).
