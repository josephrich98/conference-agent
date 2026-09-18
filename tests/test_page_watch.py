"""Offline tests for the agent-free page check (``page_watch``).

Network access is replaced by a fake session that serves canned pages, so the
link selection, error handling, and fingerprinting run hermetically.
"""

from bs4 import BeautifulSoup

from conference_agent.page_watch import (
    check_url,
    date_tokens,
    dates_links,
    fingerprint,
)


def test_date_tokens_normalizes_common_formats():
    text = (
        "Abstracts due March 5, 2026. Late posters: 12th April. "
        "Meeting 2026-11-29 to Dec. 3rd 2026. Notification: 1 June"
    )
    assert date_tokens(text) == {
        "2026-03-05",
        "04-12",
        "2026-11-29",
        "2026-12-03",
        "06-01",
    }


def test_date_tokens_ignores_non_dates():
    assert date_tokens("Version 2.0, room 12, 45,000 attendees, 03/05/2026") == set()


def test_fingerprint_is_order_independent_and_sensitive_to_change():
    assert fingerprint(["2026-03-05", "06-01"]) == fingerprint(["06-01", "2026-03-05"])
    assert fingerprint(["2026-03-05"]) != fingerprint(["2026-03-12"])


def test_dates_links_picks_same_site_dates_pages_only():
    soup = BeautifulSoup(
        """
        <a href="/important-dates">Important Dates</a>
        <a href="/news/update">Latest update</a>
        <a href="/call-for-abstracts">Submit</a>
        <a href="/bylaws-update.pdf">Call for bylaws</a>
        <a href="https://other.org/deadlines">Deadlines elsewhere</a>
        <a href="/important-dates#top">Dates again</a>
        <a href="mailto:x@example.org">Abstract questions</a>
        """,
        "html.parser",
    )
    assert dates_links("https://www.meeting.org/", soup) == [
        "https://www.meeting.org/important-dates",
        "https://www.meeting.org/call-for-abstracts",
    ]


class _Resp:
    def __init__(self, status, text="", ctype="text/html"):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": ctype}


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.headers = {}

    def get(self, url, timeout, allow_redirects):
        return self.pages.get(url, _Resp(404))


_HOME = '<p>Meeting Nov 29, 2026</p><a href="/dates">Key dates</a>'


def test_check_url_fingerprints_home_and_dates_subpage():
    session = _Session({
        "https://m.org/": _Resp(200, _HOME),
        "https://m.org/dates": _Resp(200, "<p>Abstracts due May 6, 2026</p>"),
    })
    result = check_url("https://m.org/", session=session)
    assert result.fingerprint == fingerprint(["2026-11-29", "2026-05-06"])
    assert (result.dates, result.pages, result.error) == (2, 2, None)


def test_check_url_skips_missing_subpage_but_not_blocked_one():
    gone = _Session({"https://m.org/": _Resp(200, _HOME)})  # /dates -> 404
    assert check_url("https://m.org/", session=gone).fingerprint == fingerprint(["2026-11-29"])

    blocked = _Session({
        "https://m.org/": _Resp(200, _HOME),
        "https://m.org/dates": _Resp(403),
    })
    result = check_url("https://m.org/", session=blocked)
    assert result.fingerprint is None
    assert "403" in result.error


def test_check_url_is_inconclusive_without_dates_or_page():
    empty = _Session({"https://m.org/": _Resp(200, "<p>Loading...</p>")})
    assert check_url("https://m.org/", session=empty).error == "no dates found"

    down = _Session({})
    assert check_url("https://m.org/", session=down).error == "HTTP 404"
