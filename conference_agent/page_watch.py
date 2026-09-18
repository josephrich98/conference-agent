"""Cheap, agent-free change detection for a conference's official pages.

The watch cadence (``refresh.run_watch``) uses this to decide whether a series
is worth an LLM discovery run. Rather than hashing a page's raw HTML -- which
changes on every fetch for many sites (session tokens, rotating banners, news
tickers) -- it extracts the set of calendar dates the page mentions and hashes
that. A deadline extension, a moved meeting, or a newly announced edition all
change that set; cosmetic edits do not.

Deadlines often live on a sub-page rather than the home page (a survey of the
table found the stored dates on only about half of the official links), so the
check also reads up to :data:`MAX_SUBPAGES` same-site links whose text or URL
looks like a dates page ("Important Dates", "Deadlines", "Call for Abstracts",
"Submission").

A check is *inconclusive* (``fingerprint is None``) when a page cannot be
fetched or mentions no dates at all (e.g. a page rendered by JavaScript). The
caller then falls back to a time-based agent run instead of trusting the page.

Uses ``requests`` + ``beautifulsoup4`` (the ``discover`` extra), imported lazily
so the rest of the package does not depend on them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional
from urllib.parse import urldefrag, urljoin, urlparse

# Browser-like, but identifies the project; several society sites reject
# requests that carry no browser token.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 conference-agent/1.0"
)
TIMEOUT_SECONDS = 20
MAX_SUBPAGES = 3
# Pages larger than this are truncated before parsing (defensive bound).
_MAX_BYTES = 3_000_000

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
)}
_MON = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?:,?\s+(20\d\d))?"
# "March 5", "Mar. 5th, 2026"
_MONTH_DAY = re.compile(rf"\b{_MON}\s+{_DAY}\b{_YEAR}", re.IGNORECASE)
# "5 March", "5th of March 2026"
_DAY_MONTH = re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MON}\b{_YEAR}", re.IGNORECASE)
# "2026-03-05", "2026/03/05"
_ISO = re.compile(r"\b(20\d\d)[-/](\d{1,2})[-/](\d{1,2})\b")

# A link worth following for deadlines: matched against its text and its URL.
# "dates" must be a whole word, or every "Update" / "Candidate" link would match.
_DATES_LINK = re.compile(r"\bdates?\b|deadline|call|abstract|submi", re.IGNORECASE)
# Links to documents rather than pages are not followed.
_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip")
# Sub-page statuses that mean "this page does not exist": a stable state (a
# broken link on the conference's own site), so the sub-page is ignored rather
# than making every check inconclusive. Any other failure -- 403, 429, 5xx, a
# timeout -- may be transient and does make the check inconclusive.
_GONE_STATUSES = ("HTTP 404", "HTTP 410")


@dataclass(frozen=True)
class PageCheck:
    """Outcome of checking one conference link.

    ``fingerprint`` is ``None`` when the check is inconclusive; ``error`` then
    says why. ``dates`` is the number of distinct dates found and ``pages`` the
    number of pages read (the link itself plus any sub-pages).
    """

    fingerprint: Optional[str]
    dates: int = 0
    pages: int = 0
    error: Optional[str] = None


def _token(month: int, day: int, year: Optional[str]) -> Optional[str]:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"


def date_tokens(text: str) -> set[str]:
    """The distinct dates mentioned in ``text``, normalized.

    Each date becomes ``YYYY-MM-DD`` when a year is attached to it and ``MM-DD``
    otherwise (important-dates pages often omit the year). Numeric forms other
    than ISO (``03/05/2026``) are ignored: their day/month order is ambiguous.
    """
    out: set[str] = set()
    for m in _MONTH_DAY.finditer(text):
        tok = _token(_MONTHS[m.group(1)[:3].lower()], int(m.group(2)), m.group(3))
        if tok:
            out.add(tok)
    for m in _DAY_MONTH.finditer(text):
        tok = _token(_MONTHS[m.group(2)[:3].lower()], int(m.group(1)), m.group(3))
        if tok:
            out.add(tok)
    for m in _ISO.finditer(text):
        tok = _token(int(m.group(2)), int(m.group(3)), m.group(1))
        if tok:
            out.add(tok)
    return out


def fingerprint(tokens: Iterable[str]) -> str:
    """Order-independent SHA-256 of a set of date tokens."""
    joined = "\n".join(sorted(set(tokens)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _same_site(a: str, b: str) -> bool:
    def host(u: str) -> str:
        h = urlparse(u).netloc.lower()
        return h[4:] if h.startswith("www.") else h

    return host(a) == host(b)


def dates_links(base_url: str, soup, limit: int = MAX_SUBPAGES) -> List[str]:
    """Same-site links on ``soup`` that look like a dates / deadlines page.

    Returned in document order, de-duplicated, excluding the page itself.
    """
    base = urldefrag(base_url)[0]
    out: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if not url.startswith(("http://", "https://")) or url == base or url in out:
            continue
        if not _same_site(url, base_url):
            continue
        if urlparse(url).path.lower().endswith(_DOCUMENT_SUFFIXES):
            continue
        if _DATES_LINK.search(a.get_text(" ")) or _DATES_LINK.search(urlparse(url).path):
            out.append(url)
            if len(out) >= limit:
                break
    return out


def _fetch_soup(session, url: str):
    """GET ``url`` and return ``(soup, error)``; scripts/styles are stripped."""
    from bs4 import BeautifulSoup

    try:
        resp = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=True)
    except Exception as exc:  # network errors of every kind are "inconclusive"
        return None, type(exc).__name__
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}"
    ctype = resp.headers.get("Content-Type", "")
    if ctype and "html" not in ctype.lower():
        return None, f"not HTML ({ctype.split(';')[0]})"
    soup = BeautifulSoup(resp.text[:_MAX_BYTES], "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup, None


def check_url(url: str, session=None) -> PageCheck:
    """Fingerprint the dates on ``url`` and its dates-like sub-pages.

    A failure to fetch the link itself, or a possibly transient failure on a
    chosen sub-page (403, 429, 5xx, timeout), makes the result inconclusive
    rather than fingerprinting a partial page set, so a sub-page that times out
    once does not register as a change. A sub-page that is gone (404/410) or is
    not HTML is a stable condition and is simply skipped.
    """
    import requests

    own = session is None
    if own:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
    try:
        soup, error = _fetch_soup(session, url)
        if soup is None:
            return PageCheck(None, error=error)
        tokens = date_tokens(soup.get_text(" "))
        pages = 1
        for sub in dates_links(url, soup):
            sub_soup, error = _fetch_soup(session, sub)
            if sub_soup is None:
                if error in _GONE_STATUSES or error.startswith("not HTML"):
                    continue
                return PageCheck(None, pages=pages, error=f"sub-page {sub}: {error}")
            tokens |= date_tokens(sub_soup.get_text(" "))
            pages += 1
        if not tokens:
            return PageCheck(None, pages=pages, error="no dates found")
        return PageCheck(fingerprint(tokens), dates=len(tokens), pages=pages)
    finally:
        if own:
            session.close()


# Signature of the checker ``refresh.plan_watch`` calls; injectable for tests.
Checker = Callable[[str], PageCheck]
