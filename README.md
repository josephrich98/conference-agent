# conference_agent

Website: https://conferenceagent.vercel.app
Data: https://conferenceagent.vercel.app/data/conferences.json

Dynamic academic conference database with a focus on artificial intelligence, medicine, and genomics.

Features:
- Keep up to date with conference news such as venue announcements, submission deadline posting, and submission deadline extension via calendar events and email reminders
- Organize conferences by category and subcategory, size, submission deadlines, and more
- Search with keywords (e.g. "radiology november") or boolean logic
- Sort conferences by those with the nearest submission deadlines, color-coded by timeframe
- See deadline times in your current timezone
- Read each conference's abstract and paper submission guidelines (word or page limits, required structure, templates)
- View conferences that have a history of extending deadlines
- Suggest a new conference or an edit to an existing one with the [form](https://conferenceagent.vercel.app/add/)
- Read any conference or single field as static JSON, with no API key (see [Data access](#data-access-for-scripts-and-ai-agents))

## Install

```bash
pip install conference-agent
```

## Usage

```bash
conference-agent lookup    # query the database
conference-agent info      # show one conference by id or acronym
```

Both commands search the live site's data
(https://conferenceagent.vercel.app/data/conferences.json, cached in
`~/.cache/conference-agent`), so they need no local database. The site is
checked for a newer catalog on the first run of each day; later runs that day
read the cached copy without a network request (`--refresh` checks again).

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
conference-agent lookup --columns name
```

### conference-agent info

Show one conference's entire row, found by its id (the unique name slug) or its
acronym:

```bash
conference-agent info radiological-society-of-north-america-annual-meeting
conference-agent info RSNA
```

Show only specific columns (a single column prints just its value):

```bash
conference-agent info RSNA name upcoming_start_date upcoming_location
```

If an acronym is shared by several conferences, `info` lists their ids and
exits with an error; rerun it with the id of the one you want. List ids with
`conference-agent lookup --columns id`.

To add a conference or correct one, use the
[form](https://conferenceagent.vercel.app/add/); submissions are reviewed
before they reach the table. Maintaining or self-hosting the database (the
`add`, `delete`, and `discover` commands, which come only with a development
install from a clone of the repository, and the scheduled updates) is
covered in [DEVELOPMENT.md](https://github.com/josephrich98/conference-agent/blob/main/DEVELOPMENT.md).

## Data access for scripts and AI agents

No API key or server: the live site serves the data as static JSON. The `<id>` is the
conference name slug; find it
by searching `acronym`/`name` in the catalog.

- One conference (all fields): `https://conferenceagent.vercel.app/c/<id>/data.json`
- Whole catalog (`conferences` list plus `fields` metadata): `https://conferenceagent.vercel.app/data/conferences.json`
- Agent guide: `/llms.txt`
- Calendar feeds: `/c/<id>/calendar.ics`, `/field/<tag>/calendar.ics`, `/calendar.ics`

```bash
# AUA abstract deadline date and time of day
curl -s https://conferenceagent.vercel.app/c/american-urological-association-annual-meeting/data.json \
  | jq '{date: .upcoming_abstract_deadline, time: .abstract_time, zone: .abstract_timezone}'
# AUA by acronym (acronyms are not unique; check `name`)
curl -s https://conferenceagent.vercel.app/data/conferences.json \
  | jq '.conferences[] | select(.acronym=="AUA")'
```