# conference_agent

An AI agent that automatically compiles a table of major conferences — with
abstract/submission deadlines, conference dates, location, requirements, link,
and an importance tier — and syncs them with Google Calendar.

> **Status:** early scaffolding. The package layout and schema are in place;
> the discovery agent, database, and calendar sync are stubs pending
> implementation.

## What it tracks

Each conference record captures:

| Field | Example |
| --- | --- |
| Name / acronym | Radiological Society of North America / `RSNA` |
| Abstract deadline | 2026-04-08 |
| Conference dates | 2026-11-29 – 2026-12-03 |
| Location | Chicago, IL, USA |
| Requirements | Abstract submission; member or non-member registration |
| Link | https://www.rsna.org/annual-meeting |
| Tier (status) | `big` |

Importance tier is a controlled value (`big` / `medium` / `small`) — e.g. RSNA
is `big`, SPR is `medium` — so calendar views can filter and color consistently.

## How it works (planned)

1. **Discover** — an LLM agent (Anthropic API + web search) finds conferences
   and extracts their structured fields.
2. **Store** — records are normalized to a typed schema and upserted into a SQL
   database (idempotent on acronym + year).
3. **Sync** — each conference and its deadlines are pushed to Google Calendar as
   events with stable ids, so re-syncing updates rather than duplicates.

## Install

```bash
pip install -e ".[dev]"        # core + test tooling
pip install -e ".[discover]"   # discovery agent helpers
pip install -e ".[calendar]"   # Google Calendar client
```

## Configuration

- `ANTHROPIC_API_KEY` — required for the discovery agent.
- `credentials.json` — Google OAuth client secret (from Google Cloud Console);
  the first calendar sync produces a cached `token.json`. Both are gitignored.

## License

MIT
