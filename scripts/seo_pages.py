"""Prerendered, crawlable pages for the static site (search-engine discoverability).

The main table is rendered by JavaScript, so a crawler sees almost nothing.
This module writes plain HTML that does not need JavaScript:

* ``c/<slug>/index.html`` -- one page per conference series (deadlines, dates,
  registration, cost) with schema.org ``Event`` JSON-LD when an upcoming edition
  has a start date;
* ``field/<slug>/index.html`` -- one page per category and subcategory listing
  its conferences, ordered by the next upcoming date;
* ``sitemap.xml`` and ``robots.txt``;
* the "browse" link block injected into the home page (``<!--SEO_LINKS-->``).

Everything is derived from the exported rows, so it is regenerated on each build.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

from conference_agent.models import CATEGORIES

SITE_URL = "https://conferenceagent.vercel.app"
SITE_NAME = "Conference Agent"

_STYLE = """
:root{--bg:#f7f8fa;--panel:#fff;--border:#e2e6eb;--text:#1f2933;--muted:#6b7684;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1720;--panel:#17212b;--border:#2a3644;--text:#e6ebf0;--muted:#9aa7b4;--accent:#6ea8ff}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 48px}
a{color:var(--accent)}h1{font-size:24px;margin:8px 0 4px}h2{font-size:17px;margin:24px 0 8px}
.muted{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--border);border-radius:6px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{width:34%;font-weight:600}
"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_date(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso).strftime("%B %-d, %Y")
    except ValueError:
        return iso


def _date_range(start: Optional[str], end: Optional[str]) -> Optional[str]:
    if not start:
        return None
    if end and end != start:
        return f"{_fmt_date(start)} to {_fmt_date(end)}"
    return _fmt_date(start)


def _safe_url(url: Optional[str]) -> Optional[str]:
    return url if url and re.match(r"^https?://", url, re.I) else None


def _tags(value: Optional[str]) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _label(row: dict) -> str:
    return row.get("acronym") or row.get("name") or row["id"]


def _year(row: dict) -> Optional[str]:
    start = row.get("upcoming_start_date") or row.get("prior_start_date")
    return start[:4] if start else None


def _page(title: str, description: str, path: str, body: str, jsonld: Iterable[dict] = ()) -> str:
    canonical = f"{SITE_URL}{path}"
    ld = "".join(
        '<script type="application/ld+json">'
        + json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
        + "</script>"
        for o in jsonld
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_e(title)}</title>"
        f"<meta name=\"description\" content=\"{_e(description)}\">"
        f"<link rel=\"canonical\" href=\"{_e(canonical)}\">"
        f"<meta property=\"og:type\" content=\"website\"><meta property=\"og:title\" content=\"{_e(title)}\">"
        f"<meta property=\"og:description\" content=\"{_e(description)}\"><meta property=\"og:url\" content=\"{_e(canonical)}\">"
        f"<style>{_STYLE}</style>{ld}"
        "<script defer src=\"/_vercel/insights/script.js\"></script></head>"
        f"<body><main><p class=\"muted\"><a href=\"/\">{SITE_NAME}</a></p>{body}</main></body></html>\n"
    )


def _feed_links(page_path: str, what: str) -> str:
    """Links to subscribe to the ``calendar.ics`` feed next to ``page_path``.

    ``scripts/build_static.py`` writes the feed; a subscribed calendar re-fetches
    it, so changed deadlines and dates update in place.
    """
    https = f"{SITE_URL}{page_path}calendar.ics"
    webcal = "webcal:" + https.split(":", 1)[1]
    google = "https://calendar.google.com/calendar/r?cid=" + quote(webcal, safe="")
    return (
        f"<p>Subscribe to {_e(what)} in your calendar (updates automatically): "
        f'<a href="{_e(webcal)}">Apple / Outlook</a> · '
        f'<a href="{_e(google)}" rel="noopener">Google Calendar</a> · '
        f'<a href="{_e(https)}">feed URL</a></p>'
    )


def _event_jsonld(row: dict, url: str) -> Optional[dict]:
    start = row.get("upcoming_start_date")
    if not start:
        return None
    remote = row.get("remote_option")
    mode = {
        "virtual": "https://schema.org/OnlineEventAttendanceMode",
        "hybrid": "https://schema.org/MixedEventAttendanceMode",
    }.get(remote, "https://schema.org/OfflineEventAttendanceMode")
    event = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": f"{_label(row)} {_year(row) or ''}".strip(),
        "description": row.get("name") or _label(row),
        "startDate": start,
        "endDate": row.get("upcoming_end_date") or start,
        "eventAttendanceMode": mode,
        "url": url,
    }
    place = _upcoming_location(row)
    if place:
        event["location"] = {"@type": "Place", "name": place, "address": place}
    elif remote == "virtual":
        event["location"] = {"@type": "VirtualLocation", "url": _safe_url(row.get("url")) or url}
    else:
        return None  # Google requires a location; omit rather than emit invalid markup
    return event


def _upcoming_location(row: dict) -> Optional[str]:
    """The upcoming edition's location; a stable series' prior one stands in."""
    if row.get("upcoming_location"):
        return row["upcoming_location"]
    return row.get("prior_location") if row.get("stable_location") else None


def conference_page(row: dict, generated: str) -> tuple[str, str]:
    """Return ``(path, html)`` for one conference series."""
    path = f"/c/{slugify(row['id'])}/"
    label = _label(row)
    year = _year(row)
    full = row.get("name") or label
    site = _safe_url(row.get("url"))

    rows: list[tuple[str, Optional[str]]] = [
        ("Abstract deadline", _fmt_date(row.get("upcoming_abstract_deadline"))),
        ("Late abstract deadline", _fmt_date(row.get("upcoming_late_abstract_deadline"))),
        ("Paper deadline", _fmt_date(row.get("upcoming_paper_deadline"))),
        ("Deadline time", row.get("deadline_time")),
        ("Conference dates", _date_range(row.get("upcoming_start_date"), row.get("upcoming_end_date"))),
        ("Registration", row.get("upcoming_registration")),
        ("Location", _upcoming_location(row)),
        ("Attendance format", row.get("remote_option")),
        ("Cost", row.get("upcoming_cost")),
        ("Submission types", row.get("format")),
        ("Fields", row.get("subcategory")),
        (
            "Annual attendance",
            f"{row['attendance']:,}" + (f" ({row['attendance_year']})" if row.get("attendance_year") else "")
            if row.get("attendance")
            else None,
        ),
    ]
    prior: list[tuple[str, Optional[str]]] = [
        ("Abstract deadline", _fmt_date(row.get("prior_abstract_deadline"))),
        ("Late abstract deadline", _fmt_date(row.get("prior_late_abstract_deadline"))),
        ("Paper deadline", _fmt_date(row.get("prior_paper_deadline"))),
        ("Conference dates", _date_range(row.get("prior_start_date"), row.get("prior_end_date"))),
        ("Registration", row.get("prior_registration")),
        ("Location", row.get("prior_location")),
        ("Cost", row.get("prior_cost")),
    ]

    def table(items):
        body = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in items if v)
        return f"<table>{body}</table>" if body else ""

    upcoming_tbl = table(rows)
    prior_tbl = table(prior)
    parts = [f"<h1>{_e(label)}{' ' + _e(year) if year else ''} deadlines and dates</h1>"]
    parts.append(f"<p>{_e(full)}.</p>")
    if site:
        parts.append(f'<p><a href="{_e(site)}" rel="noopener">Official conference website</a></p>')
    parts.append("<h2>Upcoming edition</h2>")
    parts.append(
        upcoming_tbl
        or "<p class=\"muted\">No upcoming edition has been announced yet. Check the prior edition below.</p>"
    )
    if prior_tbl:
        parts.append(f"<h2>Prior edition</h2>{prior_tbl}")
    parts.append(_feed_links(path, f"{label}'s deadlines and dates"))
    parts.append(
        f'<p><a href="/?q={_e(label)}">Add {_e(label)} to your calendar or get update emails</a> '
        "from the main table.</p>"
    )
    parts.append(f'<p class="muted">Data last updated {_e(generated)}. Confirm dates on the official site.</p>')

    tags = _tags(row.get("subcategory"))
    title = f"{label} {year + ' ' if year else ''}abstract deadline, dates & registration | {SITE_NAME}"
    desc_bits = [f"{label} ({full})"]
    if row.get("upcoming_abstract_deadline"):
        desc_bits.append(f"abstract deadline {_fmt_date(row['upcoming_abstract_deadline'])}")
    if row.get("upcoming_start_date"):
        desc_bits.append(f"conference {_date_range(row['upcoming_start_date'], row.get('upcoming_end_date'))}")
    if tags:
        desc_bits.append(", ".join(tags))
    description = "; ".join(desc_bits)[:300]
    ld = _event_jsonld(row, f"{SITE_URL}{path}")
    return path, _page(title, description, path, "".join(parts), [ld] if ld else [])


def _sort_key(row: dict):
    v = row.get("upcoming_start_date") or row.get("prior_start_date")
    return (v is None, v or "", _label(row))


def _field_groups(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        for tag in _tags(row.get("category")) + _tags(row.get("subcategory")):
            groups.setdefault(tag.lower(), []).append(row)
    return groups


def field_page(tag: str, rows: list[dict], generated: str) -> tuple[str, str]:
    path = f"/field/{slugify(tag)}/"
    rows = sorted(rows, key=_sort_key)
    items = []
    for r in rows:
        bits = []
        if r.get("upcoming_abstract_deadline"):
            bits.append(f"abstract deadline {_fmt_date(r['upcoming_abstract_deadline'])}")
        when = _date_range(r.get("upcoming_start_date"), r.get("upcoming_end_date"))
        if when:
            bits.append(f"conference {when}")
        elif r.get("prior_start_date"):
            bits.append(f"last held {_fmt_date(r['prior_start_date'])}")
        items.append(
            f'<li><a href="/c/{slugify(r["id"])}/">{_e(_label(r))}</a>'
            f'<span class="muted"> {_e(r.get("name") if r.get("name") != _label(r) else "")}'
            f'{" - " + _e("; ".join(bits)) if bits else ""}</span></li>'
        )
    body = (
        f"<h1>{_e(tag.title())} Conference Agent Database</h1>"
        f"<p>{len(rows)} {_e(tag)} conference series with abstract and paper deadlines, "
        "conference dates, registration, and cost.</p>"
        f"<ul>{''.join(items)}</ul>"
        + _feed_links(path, f"every {tag} deadline and conference")
        + f'<p><a href="/?{"category" if tag in CATEGORIES else "subcategory"}={slugify(tag)}">'
        "Search and filter these in the full table</a>, "
        "add deadlines to your calendar, or get email updates.</p>"
        f'<p class="muted">Data last updated {_e(generated)}.</p>'
    )
    title = f"{tag.title()} Conference Agent Database | {SITE_NAME}"
    desc = f"Abstract deadlines, paper deadlines, dates, and registration for {len(rows)} {tag} conferences."
    return path, _page(title, desc, path, body)


def write_pages(rows: list[dict], out_dir: Path, generated: str) -> dict:
    """Write all SEO pages, sitemap, and robots.txt; return link HTML for the home page."""
    for sub in ("c", "field"):
        shutil.rmtree(out_dir / sub, ignore_errors=True)

    urls: list[str] = ["/"]
    for row in sorted(rows, key=lambda r: _label(r).lower()):
        path, doc = conference_page(row, generated)
        (out_dir / path.strip("/")).mkdir(parents=True, exist_ok=True)
        (out_dir / path.strip("/") / "index.html").write_text(doc, encoding="utf-8")
        urls.append(path)

    field_links: list[str] = []
    for tag, group in sorted(_field_groups(rows).items()):
        path, doc = field_page(tag, group, generated)
        (out_dir / path.strip("/")).mkdir(parents=True, exist_ok=True)
        (out_dir / path.strip("/") / "index.html").write_text(doc, encoding="utf-8")
        urls.append(path)
        field_links.append(f'<a href="{path}">{_e(tag)}</a>')

    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"<url><loc>{_e(SITE_URL + u)}</loc><lastmod>{generated}</lastmod></url>\n" for u in urls)
        + "</urlset>\n"
    )
    (out_dir / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8"
    )

    # Field pages link to every conference, so the home page only needs the field
    # links; they sit in a collapsed <details> (crawlable, but out of the way).
    block = (
        '<nav id="browse" aria-label="Browse conferences">'
        "<details><summary>Browse by field</summary><p>" + " · ".join(field_links) + "</p></details></nav>"
    )
    return {"browse": block, "pages": len(urls)}
