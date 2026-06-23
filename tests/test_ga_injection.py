"""The index page injects Google Analytics only when CONFERENCE_GA_ID is set.

This keeps analytics scoped to the server-rendered (AWS Lambda) deployment: the
static Vercel bundle copies index.html verbatim, so it must never carry the tag.
"""

from __future__ import annotations

import web.app as appmod


def _render(monkeypatch, ga_id):
    if ga_id is None:
        monkeypatch.delenv("CONFERENCE_GA_ID", raising=False)
    else:
        monkeypatch.setenv("CONFERENCE_GA_ID", ga_id)
    appmod._index_html_cache.clear()
    return appmod._render_index()


def test_no_analytics_by_default(monkeypatch):
    markup = _render(monkeypatch, None)
    assert "googletagmanager.com" not in markup


def test_ga_injected_when_id_set(monkeypatch):
    markup = _render(monkeypatch, "G-TEST12345")
    assert "googletagmanager.com/gtag/js?id=G-TEST12345" in markup
    assert "gtag('config', 'G-TEST12345')" in markup
    # Inserted before the single closing head tag, not duplicated.
    assert markup.count("</head>") == 1


def test_invalid_ga_id_ignored(monkeypatch):
    markup = _render(monkeypatch, "'; drop table --")
    assert "googletagmanager.com" not in markup
