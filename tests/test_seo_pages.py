"""Prerendered SEO pages: crawlable HTML, sitemap, escaping, Event JSON-LD."""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from seo_pages import conference_page, write_pages  # noqa: E402

ROW = {
    "id": "X<1>",
    "acronym": "X<1>",
    "name": "Example & Co Conference",
    "category": "medicine, artificial intelligence",
    "subcategory": "radiology, machine learning",
    "location": "Boston, MA",
    "remote_option": "hybrid",
    "url": "javascript:alert(1)",
    "upcoming_abstract_deadline": "2026-02-12",
    "upcoming_start_date": "2026-09-27",
    "upcoming_end_date": "2026-10-01",
}


def test_conference_page_escapes_and_has_event_jsonld():
    path, doc = conference_page(ROW, "2026-09-30")
    assert path == "/c/x-1/"
    body = doc.split("<body>")[1]
    assert "X<1>" not in body and "X&lt;1&gt;" in body
    assert "javascript:" not in doc  # unsafe site URL is dropped
    ld = json.loads(re.search(r'ld\+json">(.*?)</script>', doc).group(1))
    assert ld["@type"] == "Event" and ld["startDate"] == "2026-09-27"


def test_write_pages_emits_sitemap_and_browse_links(tmp_path):
    out = write_pages([ROW], tmp_path, "2026-09-30")
    assert (tmp_path / "c/x-1/index.html").exists()
    assert (tmp_path / "field/radiology/index.html").exists()
    assert (tmp_path / "field/artificial-intelligence/index.html").exists()
    assert "/c/x-1/" in (tmp_path / "sitemap.xml").read_text()
    assert "Sitemap:" in (tmp_path / "robots.txt").read_text()
    assert 'href="/field/radiology/"' in out["browse"]
