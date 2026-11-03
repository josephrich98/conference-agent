# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

`conference_agent` is an AI agent that automatically compiles a curated table of
major academic and professional conferences and exports their deadlines and
dates as a subscribable calendar feed. For each conference series the agent
records its **subcategory** tags (one or more granular fields per series — e.g.
SPR is both radiology and pediatrics, MICCAI is radiology and machine learning)
and its **category** tags (one or more broad buckets, set independently of the
subcategories: usually from ten suggested values — humanities, social science,
medicine, biology, chemistry, physics, mathematics, stats, computer science,
artificial intelligence — though a custom one is kept), its **prior** and
**upcoming** editions (abstract deadline, late abstract deadline, paper
deadline, conference dates for
each, plus a free-text registration field per edition capturing the registration
window(s) — e.g. "Early bird: Jan 5 – Mar 1; Regular: Mar 2 – conference" — since
registration is published as periods, not a single date), a derived conference
month, abstract month, late abstract month, and paper
month (each
taken from the matching date so rows sort by season even when their years are
offset; registration, being free text, has no derived month), the official
link, remote-attendance option, a location and cost per edition (plus a
`stable_location` flag for series held in the same place every year), a sourced
annual-attendance figure, and a
size bucket derived deterministically from that figure (massive / large / medium /
small, an objective proxy for prominence — e.g. RSNA ≈ 45,000 attendees = massive). It
normalizes that information into a typed schema, stores it in a
SQL database, exposes it through a boolean-searchable web table, and serves each
conference's deadlines and dates as a credential-free iCalendar (`.ics`) feed.
The table covers medicine (radiology and ~18 other specialties),
genomics/bioinformatics, and data science. The database is the only source of
conferences: discovery surveys the subcategories already in it, and a new field
is added by adding one conference in it (`add`), after which the survey covers
it.

## Status

Core pipeline implemented: typed schema, SQLAlchemy persistence with idempotent
upserts, the LLM discovery agent (Anthropic web search + structured output), a
credential-free iCalendar (`.ics`) feed, an email notifier, and a web table
interface (boolean search + per-row calendar download). See `## Architecture`
for the design.

## Repository Layout

- `conference_agent/` — core reusable package
  - `models.py` — `Conference` pydantic schema (one record per conference *series*,
    holding prior + upcoming editions, a list of `subcategories` tags, a controlled
    list of submission/presentation `formats` (any of abstract / paper / poster /
    oral), an `attendance`
    figure, a list of `categories` tags (the broad bucket; `category` joins them), `conference_month`
    / `abstract_month` / `late_abstract_month` / `paper_month` (registration is
    free text — `registration` property, no derived month) and
    `size` / `attendance_display` / `format` / `subcategory` properties);
    `ConferenceSize` and `RemoteOption` enums; `CATEGORIES` (the suggested
    top-level vocabulary);
    `CONFERENCE_FORMATS` vocabulary; `size_for_attendance` (the size-bucketing rule),
    `normalize_categories` (the category tag parser), `normalize_subcategories`
    (the shared tag parser), and `normalize_formats` (the format-vocabulary parser).
    each deadline kind's time of day and time zone are structured per-series
    fields (`abstract_time` / `abstract_timezone`, likewise `late_abstract_*` and
    `paper_*`), from which the display text `deadline_time` is derived (see the
    design decision below)
  - `deadline_time.py` — the deadline-time vocabulary: `normalize_time` (any of
    24-hour / 12-hour / "noon" → canonical `HH:MM`), `normalize_timezone` (free
    text → a code in `TIMEZONES`, a `UTC±N` offset, or an IANA name), the display
    formatters (12- or 24-hour, identical times collapsed into one entry), and
    `parse_legacy_deadline_time` (the old free text → structured fields)
  - `config.py` — constants, the Anthropic model id, SMTP / notification
    settings, and the refresh schedule
  - `discover.py` — the AI discovery agent: web search (research) + structured
    output (extraction) to find conferences and extract typed fields. Two
    interchangeable backends run that flow: `claude-code` (default) drives the
    local `claude` CLI on the user's Claude Code subscription (the subprocess
    runs with `ANTHROPIC_API_KEY` removed so it never falls back to the metered
    API); `api` calls the Anthropic API directly and requires `ANTHROPIC_API_KEY`
  - `database.py` — SQLAlchemy ORM + idempotent ingestion/query helpers
  - `calendar_sync.py` — iCalendar (`.ics`) feed builder (RFC 5545; stable,
    deterministic event ids so a re-fetched feed updates events in place)
  - `refresh.py` — per-conference auto-check policy: decides which series are
    "due" for re-discovery (6–24-month staleness window from the last edition's
    submission deadline, biweekly re-check via a `last_checked` column) so
    `daily_update.py --cadence due` targets only them; and the page-gated
    **watch** cadence (`run_watch`, the scheduled job): tiers series by deadline
    proximity, runs the agent-free page check, and re-researches only changed
    series via the targeted `discover.refresh_conferences`
  - `airports.py` — the Airports column and the venue flag: geocodes a
    location (OpenStreetMap Nominatim, cached in `data/geocode_cache.json`),
    decides whether it is a specific venue or only a city, and lists the
    commercial airports (OurAirports, cached in `data/airports.csv`) within
    100 road miles (OSRM, cached in
    `data/drive_cache.json`; the nearest one when none is), marking all but the
    main ones minor (`mark_minor`, by Wikidata passenger counts cached in
    `data/airport_traffic.json` and road miles); `refresh_airports` fills
    rows whose location changed, and `parse_airports` / `format_airports` handle
    the stored `CODE | Name | N mi[ | minor]` lines (see the design decision
    below)
  - `page_watch.py` — agent-free change detection: fetches a conference's link
    plus a few same-site dates pages and fingerprints the set of dates mentioned
  - `notify.py` — email summary after a discovery / daily refresh
  - `subscriptions.py` — per-conference update emails to site visitors: diffs
    each series' subscriber-facing fields against the last run's snapshot
    (`data/notify_state.json`) and emails that series' subscribers the changes
    plus its updated `.ics`, with signed unsubscribe links
  - `live.py` — read-only access to the live site for `lookup` / `info`:
    downloads `<SITE_URL>/data/conferences.json` (cached in
    `~/.cache/conference-agent`; ETag-revalidated on the first run of a day,
    after which that day's runs make no request unless `--refresh` is given;
    falling back to the cached copy when offline)
    and loads it into a content-named SQLite file, so the same SQLAlchemy
    search runs over the live data. `lookup` / `info` use it unless `--local`
    or `--db` is given; the write commands always use the local `--db`
  - `cli.py` — command-line entry point (`discover` / `add` /
    `delete` / `lookup` / `info`). The maintainer commands (`discover` / `add`
    / `delete`) exist only in a development install
    (`cli.is_development_install`: the package runs from a source checkout,
    e.g. `pip install -e .`); a regular `pip install` gets only `lookup` and
    `info`, and naming a maintainer command there exits 2 with a pointer to
    the repository. `python -m conference_agent` runs the same CLI. `info ID [COL ...]` prints one series'
    row (or just the named `lookup` columns; one column prints the bare value),
    finding it by id (current or former) or, failing that, by case-insensitive
    acronym; an acronym shared by several series lists their ids and exits 1. A bare `discover` surveys every field
    (`database.discovery_subcategories`: the table's subcategories), one agent run
    per field; `--subcategory` / `--category` narrow the survey, while
    `--conference-name` / `--size` switch to re-checking matching stored series
    via `discover.refresh_conferences` (no new rows); `discover --options` lists
    the valid filter values and `lookup --columns name` the stored names. `_SCALAR_FIELDS` + `_COMPOSITE_FIELDS` is the single
    registry defining what `add` accepts; it generates the argparse flags, the
    `--csv`/`--json` column vocabulary, and the `add --fields` reference output, so a
    new field is added in one place and every input path picks it up. Entries
    are indexed by `conference_name` (see the name-index design decision): `add`
    fails if the name exists, `add --update` and `delete` fail
    if it does not, and any conflict in a batch writes nothing.
    `--update --new-conference-name` renames a series (its id follows).
    `discover --airports` looks up the Airports column instead of running the
    agent, for rows whose location changed (`--airports all` for every row;
    the discover filters narrow it). `discover --guidelines` researches the
    Abstract / Paper guidelines columns instead of surveying (see the
    guidelines design decision)
- `conference_agent/web/` — FastAPI app + static single-page table (`search.py` boolean-query
  language, `nl_query.py` optional natural-language → boolean-query translation
  via a local Ollama model, `app.py` REST API, `static/index.html`, `handler.py`
  Lambda entry point, `requirements.txt` for the Lambda build). For the
  credential-free static deployment, `static/search.js` and `static/calendar.js`
  reimplement `search.py` and `calendar_sync.py` in the browser (see the
  static-hosting design decision below)
- `scripts/` — runnable entry points (`build_table.py`, `daily_update.py`,
  `push_db.py`, `deploy.sh` one-command reconcile + deploy, `scheduled_discovery.sh`
  the daily cron job, `build_static.py` the static-site bundler,
  `notify_subscribers.py` the subscriber-email step of the cron job,
  `ingest_submissions.py` adds merged "Add or edit a conference" submissions,
  `alert_failure.py` emails the log tail when the cron job fails)
- `scripts/seo_pages.py` — prerenders crawlable per-conference (`/c/<id>/`, with
  `Event` JSON-LD) and per-field (`/field/<tag>/`) pages, `sitemap.xml`,
  `robots.txt`, and the home page's browse links; called by `build_static.py`
  (the table itself is JS-rendered, so crawlers need these). Vercel Web
  Analytics (`/_vercel/insights/script.js`) must be enabled in the project dashboard.
- `conference_agent/web/vercel/` — the Vercel Functions behind the table's ✉️ subscribe button
  (`api/subscribe.js`, `confirm.js`, `unsubscribe.js`, `subscriptions.js`,
  shared `_lib.js`) and their `package.json`; `build_static.py` copies them into
  `dist/`. `api/propose.js` backs the "Add or edit a conference" form
  (`conference_agent/web/static/add.html`, served at `/add/`, linked from the header and below
  the table): the form is generated from `data/add_fields.json`
  (`cli.add_field_schema`, the same vocabulary as `add --fields json`) but
  lays it out one line per `ROWS` entry. The dates are entered once (upcoming
  or previous edition): the page files them under the prior keys when the
  meeting has ended and no deadline is ahead (the `roll_past_editions` rule),
  else under the upcoming ones. Location, Registration, and Cost each have
  Upcoming / Prior radio buttons beside them (Location also Stable, which sets
  `stable_location`); until clicked they follow the dates. (Airports are not
  a form field: they are looked up from the location.) Category(s) and
  Subcategory(s) are independent pickers: a dropdown of the values in use (plus
  the suggested categories) or a typed custom value. An "Edit an existing
  conference" box above the form searches the listed names and acronyms as you
  type; choosing one (or opening `/add/?edit=<id>`) fills the form from its
  `conferences.json` row (the upcoming edition's dates, else the prior's), and
  the submission carries only the fields that changed (plus
  `new_conference_name` for a rename; removing a value is not supported).
  `api/propose` files an edit as `submissions/<id>-<date>-<rand>.update.json`,
  which `ingest_submissions.py` writes with `add --update --json`. A submission is validated,
  committed as `submissions/<id>-<date>-<rand>.json`
  (an `add --json` record) on a new branch, and opened as a pull request;
  nothing reaches the table until a maintainer merges it. The daily cron job
  then runs `scripts/ingest_submissions.py`, which reads new
  `submissions/*.json` from `origin/main` via `git show` (the working tree is
  untouched), runs `add --json` on each, and records the outcome in
  `data/ingested_submissions.json` (a failed file is not retried)
- `infra/` — AWS SAM deployment (`template.yaml`: CloudFront over an
  IAM-protected Lambda Function URL + RDS PostgreSQL in a VPC, with optional
  `DomainName`/`AcmCertificateArn` for a custom domain; `samconfig.toml`); built
  via the root `Makefile`. The serving Lambda injects Google Analytics when
  `GaMeasurementId` is set (AWS-only; the static site never carries it), and
  setting `EnableScheduledRefresh=true` provisions an EventBridge Scheduler +
  refresh Lambda (`conference_agent/web/refresh_handler.py`) + NAT gateway that replace the old
  GitHub Actions refresh cron. This AWS path is a private, on-demand demo kept
  torn down to $0 — see the gitignored `DEPLOY_AWS.md` run-book.
- `tests/` — offline unit tests (network/LLM tests are marked and excluded from CI)
- `data/` — generated tables / databases (gitignored, never committed)
- `.github/workflows/` — `ci.yml` only (lint + offline tests). The scheduled
  refresh workflows (`weekly_update`/`monthly_update`/`auto_check`) were replaced
  by EventBridge Scheduler in the AWS SAM stack (see `infra/template.yaml`)
- `README.md` — user guide (`lookup` / `info`, the form, static data access);
  `DEVELOPMENT.md` — maintainer guide (`add` / `delete` / `discover`, the
  local database, the site conversion, scheduled updates)
- `DEPLOY.md` — deploy guide: the static Vercel path (live) and the legacy AWS stack

## Development Setup

Use the `conference_agent` conda environment for all work in this repo:

```bash
conda activate conference_agent
```

```bash
pip install -e .                   # core: lookup / info / add / delete (no discovery)
pip install -e ".[dev]"            # core + test tooling (includes web + discover)
pip install -e ".[discover]"       # discovery: anthropic (api backend) + fetch/parse helpers
pip install -e ".[web]"            # FastAPI web table + calendar feed
```

`pyproject.toml` is the authoritative source for dependencies. Add new
dependencies there rather than installing ad hoc.

### Credentials (not committed)

- `ANTHROPIC_API_KEY` — required only for the `api` discovery backend (and the
  AWS scheduled-refresh Lambda, which uses the `api` backend). The default
  `claude-code` backend uses the local Claude Code subscription and needs no key.

## Architecture / Key Design Decisions

- **One record per conference series, indexed by name.** The `Conference`
  schema keys on the full name and holds both the **prior** and **upcoming** editions
  (abstract deadline, paper deadline, start/end dates for each). Re-running
  discovery updates the same row each cycle, rolling a newly announced edition
  into the "upcoming" columns rather than creating a second row. Keeping prior
  dates alongside upcoming lets the table show last year's schedule as a
  reference before next year's is announced.
- **LLM-driven discovery, typed output.** `discover.py` runs two phases: a
  web-search agentic loop (research) followed by a structured-output call
  (extraction) that validates against the `Conference` model. The same flow runs
  through either backend — the `api` backend uses `messages.parse`; the
  `claude-code` backend uses the CLI's `--json-schema` structured output. The
  `api` model id lives in `config.py` (default: the latest Claude model); the
  `claude-code` backend defaults to Claude Code's configured model.
- **One canonical, sortable abstract deadline plus an explicit late one.** Some
  series publish two abstract deadlines for the same edition: a poster-only
  deadline after a talk-only main one (CSHL Biological Data Science — talks Aug
  28, posters Oct 1), or a late-breaking / late-poster round after the main call
  closes (ASHG, ISMB, RECOMB). A survey of 12 genomics/ML series found 1 of the
  former and 6 of the latter, so this is common enough to model but far from
  universal. Rather than overload one column with two dates (which would break
  sorting, the `abstract_month` derivation, date comparisons in the search, and
  the `.ics` event) or add a column per presentation type (which would serve only
  the oral/poster case and not late-breaking), there is one extra date field per
  edition: `*_late_abstract_deadline`, with `late_abstract_month` derived from it
  exactly as `abstract_month` is from the main deadline. `*_abstract_deadline`
  always holds the **earliest, primary** deadline — in every case surveyed the
  earliest is also the main submission route, so sorting on it never overstates
  the time remaining. The pair is threaded through the search
  (`late_abstract_due` / `late_abstract_month`), the table (two columns), the
  calendar (a fourth event, kind `late-abstract`), the discovery prompts, and
  the manual `add` paths.
- **Deadline time of day is structured per deadline kind, and the display text
  is derived.** What matters is the time zone ("23:59 AoE" vs "11:59 PM ET" is
  nearly a day apart), and a series keeps its convention year to year, so the
  time is per series, not per edition. Six nullable columns (not shown in the
  table or the CSV) are the source of truth: `abstract_time`, `late_abstract_time`,
  `paper_time` (always 24-hour `HH:MM`) and `abstract_timezone`,
  `late_abstract_timezone`, `paper_timezone` (a code from
  `deadline_time.TIMEZONES` — `AoE`, `UTC`, region codes such as `ET`/`CET`
  that follow daylight time, so EST/EDT/"Eastern Time" all store as `ET` — or a
  `UTC±N` offset or IANA name). Input may be 12- or 24-hour and any zone
  spelling; the model validators canonicalize it, so storage never carries a
  display preference and the browser never parses free text. The user-facing
  `deadline_time` is *derived* (like `size`; stored denormalized,
  never accepted as input except as a legacy shorthand that is parsed into the
  six fields): `"11:59 PM ET"` when every deadline the series has shares one
  time (identical times show once, not "abstract: … paper: …"), otherwise one
  `kind: time` line per deadline, omitting a dated deadline whose time was not
  published. Databases predating the columns are migrated on first open
  (`database.backfill_deadline_times`). The deadline columns stay pure dates so
  sorting, the derived months, the search's date comparisons, and the all-day
  calendar events are untouched. The table's "Deadline time" column
  (substring-searchable as `deadline_time:`) has a ⚙ box with a "Military time
  (24-hour)" checkbox (display only, remembered per browser; default 12-hour),
  and the calendar feed puts the applicable time in each deadline event's note
  (`calendar_sync.deadline_time_for`, mirrored in `calendar.js`).
  **Coloring is instant:** each colored date carries the instant its deadline
  passes (`calendar.js` `deadlineInstant`: the end of its stated minute in its
  zone; with no time the end of the day, and with no usable zone AoE, the
  latest day-end anywhere), and `index.html` re-judges the colors from a timer set
  for the next such instant (plus local midnight and tab re-focus), so a date
  goes from green to red the moment it passes with no re-render or redeploy.
  The zone table and the display grouping exist in both Python and JS;
  `tests/test_deadline_time.py` pins them together via Node.
- **Submission guidelines are per-series free text, researched by their own
  pass.** `abstract_guidelines` and `paper_guidelines` (on the model, nullable
  Text columns, cleaned by `models.normalize_guidelines`) hold a call's
  requirements (word / character or page limits, structure, figures, templates,
  anonymization) as a paragraph or `- ` bullet lines; the table's two columns
  (right of Deadline time), the `/c/<id>/` pages, and `seo_pages._guidelines_html`
  render bullet lines as a list. They are `add` fields, form textareas (4,000
  characters in `api/propose`), search fields (`abstract_guidelines:`), and
  pinnable. The survey and re-check prompts do not research them (that would
  lengthen every run); a survey that reports none keeps the stored text
  (`upsert_conferences`). Instead `discover --guidelines`
  (`discover.research_guidelines`, two-phase like discovery, batches of
  `GUIDELINES_BATCH_SIZE`) covers series with an upcoming deadline still ahead
  that are due by `database.guidelines_targets`: never researched, empty and
  researched `GUIDELINES_RETRY_DAYS` (14) ago, or researched
  `GUIDELINES_RECHECK_DAYS` (120) ago; at most `GUIDELINES_MAX_PER_RUN` (15)
  per run, soonest deadline first. `record_guidelines` keeps stored text over a
  blank result and stamps the bookkeeping column `guidelines_checked`. The
  guidelines are not subscriber-watched fields, since each re-research rewords
  them.
- **Deadline extensions are derived on write (or entered by hand) and shown
  only as a marker.** Three
  text columns (`abstract_deadline_extension`, `late_abstract_deadline_extension`,
  `paper_deadline_extension`) log observed extensions as newline-separated
  `MM/DD/YYYY – MM/DD/YYYY` (original – extended) lines. They are not on the
  `Conference` model, not researched by discovery, and not table or CSV
  columns. `add` accepts history as `deadline_extensions` (a repeated
  `--deadline-extensions TYPE ORIGINAL EXTENDED` flag, `{type, original,
  extended}` objects in JSON, `;`-separated entries in a CSV cell; parsed by
  `database.parse_deadline_extensions`), which `merge_records` appends to the
  stored lines (duplicates dropped); an entry more than five years old is
  rejected. The add form's "Deadline extension history" (below the paper
  deadline) collects these as rows of type / original date / extended date,
  with a + button for more. On the site, `conference_agent/web/app.py` `_EXTENSION_COLUMNS` carries them in
  each serialized row only so `index.html` can mark the deadline dates:
  `extensionMark` puts a ⏩ after a date whose series has an extension whose
  extended date has already passed (that history on hover or tap; an extension
  still in progress is not history, so a lone current one shows no ⏩), and
  `extendedMark` adds a superscript + when the displayed date is itself an
  extended date (the extension is already granted). `database._record_extensions`
  runs in both write paths (`upsert_conferences`, `merge_records`) and appends
  a line when an upcoming deadline moves later for the same edition (start date
  within `NEW_EDITION_GAP_DAYS`), by at most `EXTENSION_MAX_DAYS` (120), and is
  observed no earlier than `EXTENSION_NOTICE_DAYS` (60) before the original
  deadline (earlier changes are treated as corrections). Entries whose extended
  date is more than five years old are pruned on write and by
  `roll_past_editions`.
- **Location and cost are per edition.** A series moves cities and changes its
  fees, so both are prior/upcoming pairs (`prior_location` / `upcoming_location`,
  `prior_cost` / `upcoming_cost`, in `models.EDITION_SUFFIXES`), rolled with the
  rest of the edition. The legacy single `location` / `cost` input keys fill the
  upcoming slot, and the model's `location` / `cost` properties are the displayed
  value (upcoming, else prior), which the subscriber diff and `lookup` use. The
  table shows a prior-edition value in orange (`.from-prior`), except a location
  of a series flagged `stable_location` (held in the same place every year, e.g.
  RSNA), which also survives a roll when the finished edition recorded none. A
  discovery run never clears that flag (`upsert_conferences`); an explicit
  `add --update --stable-location false` does. Databases with the old single
  columns are migrated on open (`database._migrate_location_cost_to_editions`:
  the value moves to the upcoming slot when the row has an upcoming date, else
  to the prior one).
- **Nearby airports and venue map links are derived from the location.** The
  Airports column (right of Location) lists the commercial airports (scheduled
  passenger service and an IATA code, from OurAirports) within
  `airports.AIRPORT_RADIUS_MILES` (100) road miles of the displayed location
  (upcoming, else prior; `database.displayed_location`), each shown as
  "CODE (N mi)", nearest first; when none is that close, the single nearest
  airport is listed (searched beyond the radius when no airport is within 100
  straight-line miles; `FALLBACK_CANDIDATES`). Road miles come from the public
  OSRM server (no key; `airports._Router`, cached in `data/drive_cache.json`).
  OurAirports places an airport mid-field, which OSRM snaps to airside service
  roads, so the slow (< 12 mph) steps ending a route are measured once per
  airport and subtracted (about 3 miles at O'Hare). An airport with no road
  route keeps its straight-line miles. Driving times are not shown, since
  traffic makes free-flow times misleading.
  The location is geocoded with Nominatim, trying a parenthetical venue folded
  in, then the full text, then dropping leading comma-separated parts toward the
  city; a match wider than 150 miles that is not a city (a state or country)
  is not used, and a value naming no single place ("TBA", "Virtual", several
  cities joined by `;` or ` / `) gets no airports. Distances run from the venue,
  or for a city-only location from the city's center (Nominatim's point for the
  city, the same place a map search for it shows). The same lookup sets
  `location_venue`: true when the full location (not a fallback) matched a
  specific place rather than a city or boundary, and its first part is not
  itself the name of a settlement (a `featureType=settlement` search: "Hengqin,
  China" matches a railway station but is a town). The table links a venue
  location to Google Maps, and each airport to Google Maps directions from the
  airport to the location (the venue, or the city center). Each list has at
  least one **main** airport; the rest are stored as **minor**
  (`airports.mark_minor`). By its largest Wikidata passenger count from the
  last ten years, an airport is a hub (≥ `HUB_AIRPORT_PASSENGERS`, 10 million),
  major (≥ `MAJOR_AIRPORT_PASSENGERS`, 500,000; or OurAirports'
  `large_airport` class when Wikidata has none), or neither. The main airports
  are those of the best tier present no farther by road than
  `max(1.5 × nearest, nearest + 10 mi)` of that tier's nearest (an airport with
  no road route is minor when any has one), plus any hub within 75 miles with
  at least twice the passengers of the busiest of those (`BUSIER_HUB_*`). So
  Cold Spring Harbor gets LGA and JFK (28 and 30 mi; Newark, about 45, is
  minor), McCormick Place gets Midway and O'Hare (11 and 21 mi), Seoul gets
  Gimpo and Incheon, and Hinxton gets Stansted and Heathrow (19 and 69 mi). The column hides minor
  airports unless the ⚙ box in its header ("Include all airports", remembered
  per browser, default off) is checked, and the `/c/<id>/` pages list only the
  main ones (the former `| small` marker still parses as minor). Airports are automatic only:
  they follow from the location, so there is no `add` field or form input (a
  wrong list is fixed by making the location more specific); `airports` in a
  CSV export is accepted on re-import and ignored. Three columns hold this, none
  on the `Conference` model and none researched by discovery: `airports` (one
  `CODE | Name | N mi[ | minor]` line each; a table, CSV, and search column,
  `airports:ORD`, which matches minor airports too), `airports_location` (the
  location they were found for), and `location_venue` (serialized for the table
  only, like the deadline times). Every write path (`upsert_conferences`,
  `merge_records`, `roll_past_editions`) runs `database._sync_airports`, which
  clears all three when the displayed location no longer equals
  `airports_location`, so a stale list is never shown; `airports.refresh_airports`
  (`conference-agent discover --airports`, run by the daily job) then looks the new
  location up. A lookup that cannot reach a service leaves the row for the next
  run.
- **Controlled vocabularies.** `ConferenceSize` (`massive`/`large`/`medium`/`small`) and
  `RemoteOption` (`in-person`/`virtual`/`hybrid`; an unknown option is NULL) are enums, not free
  text, so the table and queries can filter/color consistently.
- **Two-level classification: category and subcategory are independent inputs.**
  The granular `subcategory` (the specific field) and the broad `category` are
  both multi-valued tag columns, set separately (MICCAI → subcategories
  `radiology, machine learning`, categories `medicine, artificial intelligence`).
  `models.CATEGORIES` lists ten suggested categories, which pickers offer and
  `normalize_categories` orders first; any other value is kept as entered.
  Discovery researches the category alongside the subcategories; a run that
  reports none keeps the stored value (`upsert_conferences`). `discover --category` expands to the subcategories of
  the stored series in that category. The legacy single `category` column (granular tags) is renamed to
  `subcategory` in place on first open by `database._migrate_category_to_subcategory`.
- **Deterministic, sourced size (not a subjective reputation label).** Size is a
  computed property, never a stored hand-set value: `models.size_for_attendance`
  buckets the `attendance` integer (≥10,000 → massive, ≥1,000 → large, ≥250 →
  medium, else small;
  blank when attendance is unknown — thresholds live in `models.py`). The stored `size` column is only ever written
  by that function, so it can never drift from the figure. Each attendance figure
  carries the year it describes and the source URL it came from (provenance kept
  internal — not exposed via the API/CSV); the table shows it as e.g.
  `45,000 (2025)`.
- **SQLAlchemy over raw SQL.** The same ORM runs against SQLite (local) or any
  SQLAlchemy backend with only a connection-string change.
- **Name index; the id is the name's slug.** Two series can share an acronym
  (ICML is also a lymphoma meeting), so the name is the key: `Conference.id` is
  `models.name_id(name)`, a lowercase hyphenated ASCII slug (case, accents,
  punctuation, and spacing do not distinguish names). The id is what calendar
  UIDs, stored email subscriptions, `data/notify_state.json`, and the `/c/<id>/`
  pages carry, so when an id changes (a rename via `database.rename_conference`,
  or the one-time move off upper-cased acronym ids done on open by
  `database._migrate_ids_to_names`) the former id is recorded in the
  `id_aliases` table. `resolve_ids` maps former ids forward: the subscriber
  step uses it for snapshot entries and Blob subscriptions stored under old ids
  (unsubscribe links keep naming the stored id), `merge_records` accepts a
  former `id`, and `build_static.py` writes `dist/vercel.json` with permanent
  redirects from each former `/c/<id>/` page. A rename does change that series'
  calendar UIDs, so a re-imported `.ics` adds new events.
- **Idempotent ingestion; discovery never renames.** Upserts match an existing
  row by name id (or a former id); failing that, by the same acronym with an
  overlapping subcategory, because the agent does not always reproduce a stored
  name verbatim (`database.match_row`). A same-acronym series in an unrelated
  field becomes its own row. The stored name is kept on every match; only an
  explicit rename changes it. The prompts ask the agent to reuse the listed
  names, and `discover.refresh_conferences` returns results under the targets'
  stored names. A survey's prompt lists the stored conferences in that field
  as a checklist to cover (`discover._known_checklist`).
- **Pinned fields override discovery, per conference.** Any series can lock
  fields so no discovery path changes them: the `pinned` column holds stored
  column names (`database.PINNABLE_FIELDS`, the researched inputs), set with
  `add [--update] --pin FIELD...` / `--unpin FIELD...` using `add`'s field names
  (`cli.pin_targets`; `conference_dates` pins both dates, `deadline_time` all
  six time fields). `upsert_conferences` (a survey) and
  `apply_refreshed_conferences` (a re-check) restore pinned values and re-derive
  what is computed from them (`_restore_pins`); manual writes (`add`,
  `merge_records`, `--overwrite`, which calls `upsert_conferences(manual=True)`)
  still apply. Discovery starts from the stored link (the page watch fetches
  it; a re-check passes it to the agent), but the agent may report another, so
  pin `url` to keep a verified one. The former seed list's hand-checked links,
  hard-coded CSHL formats, and curated subcategories were converted to pins.
- **Idempotent calendar feed.** Each event carries a deterministic id derived
  (base32hex) from the conference id and event kind, so a re-fetched feed updates
  existing events instead of creating duplicates. A conference yields up to four
  events for its upcoming edition (abstract, late abstract, and paper deadlines,
  and the conference dates), or for its prior edition when no upcoming date is
  known (`calendar_sync.calendar_edition`, mirrored in `calendar.js`), so a
  finished edition stays in subscribed calendars until the next is announced.
  The feed is pure-Python iCalendar (RFC 5545), so it needs no
  credentials and runs from the static/Lambda web layer. All-day reminders are
  anchored to the morning (see `calendar_sync._alarm_trigger`) so calendar apps
  label "N days before" correctly rather than a day early.
- **Boolean-searchable web table.** `conference_agent/web/` mirrors a proven pattern: a small
  boolean query language (`field:value`, `AND`/`OR`/`NOT`, parentheses, date
  comparisons) compiled to SQLAlchemy filters, a FastAPI JSON/CSV API, and a
  static single-page table with a per-row "📅 cal" calendar-download button and a
  "Subscribe (.ics)" feed URL that mirrors the active search. One search box
  serves both modes: the exact boolean search runs first, and when it matches
  nothing (or the text does not parse) the page falls back to
  `keywordSearch` in `conference_agent/web/static/search.js`, a forgiving, relevance-ranked
  keyword match (prefix/stem/typo tolerant, filler words dropped, rows matching
  more terms first, ties going to the larger meeting by attendance). A month
  name counts most for the meeting's month (`conference_month`) and less for a
  deadline month, unless the query mentions a deadline (`deadline`, `due`,
  `submission`, `abstract`, `paper`, ...), which reverses that, so "radiology
  november" lists RSNA first. The fallback is not in the API; its Python port,
  `conference_agent.web.search.keyword_search`, serves `conference-agent lookup` (unique column
  values over the same search), so the boolean grammar, its Python parity, and
  the API are unchanged; `tests/test_keyword_search.py` covers it via Node and
  pins the Python port to it. Categorical
  column headers (category, subcategory, format, size, remote, the four month
  columns) carry an Excel-style ■ checkbox value filter; clicking a cell (or
  one tag in it) in those columns opens the same list beside it with that value
  pre-checked. The chosen values are
  browser-only state held apart from the search box and ANDed with it, so they
  survive a new search and the boolean grammar (which has no exact-match
  operator for text) stays unchanged. An ascending sort on Abstract / Late abstract /
  Paper due or Conference dates hides rows whose shown date (upcoming, else
  prior; a meeting's last day) is before today, and the status line counts
  them; the ⚙ box on those four headers has "Include past deadlines"
  (remembered per browser, default off) to keep them. Descending sorts and
  relevance order are unaffected, and rows with no date stay (sorted last). A "Browse by field" bar above the search box (one
  card per category with rows, then the chosen category's subcategories as
  chips: every subcategory tagging a series in that category) sets those
  same category / subcategory filters, and the page mirrors them and the
  search text in the URL (`/?category=<slug>`, `/?subcategory=machine-learning`,
  `&q=...`; back/forward step through selections), so a field's view is a
  shareable link. The `/field/<tag>/` pages link to these URLs.
- **Optional natural-language search over a local LLM (dynamic backend only).**
  The table has no AI search button; this survives only as the AWS backend's
  `/api/translate` endpoint. `conference_agent/web/nl_query.py`
  translates a plain-English request into the boolean query language above using
  a free, local Ollama model (no API key, no external network call). The system
  prompt's field list and controlled vocabularies are derived from
  `conference_agent.web.search.field_help`, so they never drift from what the parser accepts; the
  model's output is validated with `build_filter` (with one repair round) before
  it is returned, so the search box is never populated with a query that errors.
  The feature is optional and degrades gracefully — `GET /api/translate` returns
  503 when no model is running, and the manual boolean box still works. It is a
  web-layer concern only, so the core `conference_agent` package takes no new
  dependency (the Ollama HTTP call uses the standard library).
- **Separation of concerns.** Discovery, persistence, the calendar feed, email
  notification, and the web layer are independent modules; each can run on its
  own schedule.
- **Per-conference update emails (the one piece of server compute).** The ✉️
  subscribe button next to 📅 cal asks for an email address (remembered in
  `localStorage`, so later it is prefilled and Enter subscribes) and POSTs to the
  Vercel Function `/api/subscribe`. Subscriptions are double opt-in: the first
  time, a signed confirmation link (HMAC over email + id + expiry, keyed by
  `SUBSCRIBE_SECRET`) is emailed and nothing is stored until a person clicks
  Confirm on the linked page (GET only renders a button, because mail scanners
  prefetch links). Confirming writes `subs/<base64url(email)>/<id>` to a
  **private** Vercel Blob store and sets an HttpOnly `ca_verified` cookie, so
  later subscriptions for that address from the same browser apply without
  another email. Confirmation emails are capped at 25 per address per day. The
  sending half runs locally: after each cron refresh (and redeploy),
  `scripts/notify_subscribers.py` reads the list from the bearer-protected
  `/api/subscriptions`, and for each series whose watched fields
  (`subscriptions.WATCHED_FIELDS`) changed, emails its subscribers the changes
  with the series' `.ics` attached (same UIDs as the site, so re-importing
  updates events in place) and RFC 8058 one-click unsubscribe links. A series'
  snapshot advances only after its sends succeed, so failures retry next run.
  A finished edition moving to prior (below) is not reported as a change
  (`subscriptions.drop_finished_edition`). The same step sends each subscriber
  one reminder per upcoming deadline once it is `SUBSCRIBER_REMINDER_DAYS` (7)
  days away or closer (`subscriptions.send_reminders`; per-address state in
  `data/reminder_state.json`).
  The tokens must match between `conference_agent/web/vercel/api/_lib.js` and
  `subscriptions.sign` (a test pins a JS-computed value). The functions run only
  on subscribe / confirm / unsubscribe, never for browsing or search.
- **Static, compute-free hosting path.** The deployed site is read-only
  (discovery/ingestion run offline), so it can be served as static files with no
  per-request compute — there is no Lambda to invoke and no database to keep
  running, so query volume cannot incur cost. `scripts/build_static.py` snapshots
  the database to `dist/data/conferences.json` and copies the single-page UI into
  `dist/`; the browser does search, sort, CSV export, and per-row `.ics`
  generation entirely client-side. `conference_agent/web/static/search.js` ports the boolean query
  language from `conference_agent/web/search.py` (compiling to a row predicate instead of a SQL
  filter), and `conference_agent/web/static/calendar.js` ports `conference_agent/calendar_sync.py`
  (matching its base32hex UIDs so re-downloaded events stay idempotent). The risk
  of the two search implementations drifting is guarded by
  `tests/test_search_parity.py`, which asserts the JS and Python searches select
  identical rows over a query corpus (skipped when `node` is absent, e.g. in CI).
  Subscribable feeds are static files too: `build_static.py` writes
  `c/<id>/calendar.ics`, `field/<tag>/calendar.ics`, and `calendar.ics` with the
  Python builder (served as `text/calendar` via `vercel.json`); the 📅 button
  offers them (webcal / Google / copy URL) alongside the one-time download. The
  search-mirroring feed of the dynamic backend is not available statically.
  Each conference's full row is also a static `c/<id>/data.json` (CORS-enabled),
  so a script can fetch one series or one cell (`jq .upcoming_abstract_deadline`)
  without downloading the whole catalog; `llms.txt` (written by `seo_pages.write_pages`)
  describes these endpoints for AI agents.
  The AWS SAM stack remains as the alternative dynamic backend.

## CI/CD

- `ci.yml`: `ruff check` + `pytest -m "not network and not llm"` on push/PR.
  Committed tests are offline; mark any test that hits the network with
  `@pytest.mark.network` and any test that calls the Anthropic API with
  `@pytest.mark.llm` so CI stays hermetic.

## Deployment

- **"Deploy" means the static bundle to Vercel (the live path), not AWS.** When
  the user asks to deploy, build and ship the static site:

  ```bash
  python scripts/build_static.py                 # snapshot DB + UI into dist/
  ( cd dist && npx vercel deploy --prod --yes )  # publish to the conferenceagent project
  ```

  `scripts/deploy_static.sh` wraps exactly these two steps (activating the
  conda env if needed), so `bash scripts/deploy_static.sh` is the one-command
  deploy. The Vercel CLI must already be logged in, or `VERCEL_TOKEN` set.

  The live site is `https://conferenceagent.vercel.app`, served as static files
  directly from Vercel — no per-request compute and no AWS in the request path.
  `dist/` is linked (via `dist/.vercel/project.json`) to the Vercel project
  **conferenceagent**; deploy only from `dist/`. Cloudflare Pages
  (`npx wrangler pages deploy dist`) is an equivalent static host if ever needed.
  To refresh the live data, re-run `build_static.py` after a discovery run and
  redeploy — there is no database to push. The export omits **retired** series
  (`refresh.is_retired`: no future edition and the last one is more than
  `CHECK_WINDOW_MAX_MONTHS` old, i.e. past the point auto-checks stop); rows are
  never deleted, so a series reappears once discovery records a new edition.
  `--include-retired` exports everything.
- **Legacy AWS path (no longer the deploy target).** `scripts/deploy.sh` (push
  local DB → RDS; `DEPLOY_CODE=1` also `sam build` + `aws lambda
  update-function-code`) and the `deploy/vercel/` proxy (a rewrite to CloudFront)
  belong to the retired AWS stack documented in `DEPLOY.md`. Do **not** run them
  for a normal deploy — `deploy/vercel/` is linked to the same Vercel project, so
  deploying from it would revert the public URL to proxying AWS. Use them only
  when explicitly working on the AWS stack.
- **Automatic refresh.** A local cron entry (`0 2 * * *`) runs
  `scripts/scheduled_discovery.sh` daily. It runs `daily_update.py --cadence
  watch`, which page-checks series by tier (deadline within ±14 days: daily; a
  deadline or meeting within 30 days, under 6 months since the last edition
  ended with no next one, or 6–24 months since the last edition: every 14 days)
  and calls the agent only for series whose pages' dates changed (or that are
  unreadable / past a 28-day backstop). Before planning and after research it
  runs `database.roll_past_editions`, which moves an upcoming edition whose
  meeting is over (and with no deadline still ahead) into the prior slots and
  merges an edition stored in both slots (the `Conference` model collapses the
  same duplicate on input). Before the watch run the job ingests merged
  submissions, and after it runs `conference-agent discover --airports` for new or
  changed locations and `conference-agent discover --guidelines --model sonnet`
  for the submission guidelines that are due; if any step fails it emails the log tail via
  `scripts/alert_failure.py`. It redeploys (via
  `scripts/deploy_static.sh`) only when the exported site data changed; the DB
  file itself changes every run from bookkeeping columns, so it is not the
  comparison. cron's minimal PATH lacks `~/.local/bin` (`claude`) and nvm
  (`vercel`); the script adds both, and sources email secrets (`SMTP_USER`,
  `SMTP_PASSWORD`, `SUBSCRIBE_SECRET`) from
  `~/.config/conference-agent/secrets.env` (outside the repo). After the
  redeploy it runs `scripts/notify_subscribers.py` (see the update-emails design
  decision). Logs: `data/logs/`.
- **Update-email setup (Vercel).** The functions need a private Blob store
  connected to the **conferenceagent** project and the env vars
  `SUBSCRIBE_SECRET` (same value as the local secrets file), `SMTP_USER`, and
  `SMTP_PASSWORD` (optionally `SMTP_HOST` / `SMTP_PORT`) in its Production
  environment.
- **Add-a-conference setup (Vercel).** `api/propose` needs `GITHUB_TOKEN` (a
  fine-grained token on the repository with Contents and Pull requests
  read/write) in the Production environment; `GITHUB_REPO` / `GITHUB_BASE`
  override the default `josephrich98/conference-agent` / `main`. It uses the
  same Blob store to rate-limit submissions (10 per client address and 100 in
  total per day).

## Conventions

- Generated databases/tables (`*.db`, `*.sqlite`, `*.sql`) and the `data/`
  directory are gitignored and must never be committed.
- Secrets (`ANTHROPIC_API_KEY`, SMTP credentials) are never committed and never
  hard-coded.
- Use American English spelling. Write with a professional tone. Do not overstate
  claims.

## Working with the Anthropic API

When implementing or modifying `discover.py` (or any code that calls Claude),
consult the `claude-api` skill for current model ids, tool-use patterns, and
structured-output guidance rather than relying on memory. The model id is
centralized in `conference_agent/config.py`.
