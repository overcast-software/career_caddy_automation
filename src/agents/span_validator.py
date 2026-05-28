"""Post-LLM span-atomic validation for digest emails (Leg 1).

When the email body contains multiple job mentions, the LLM extractor
occasionally pairs a link from one row with the title/company from
another (the jp 1724 ZipRecruiter / SNBL incident). After extraction we
re-anchor each ``JobLink`` against the email body: the link's host must
co-occur with the title or company text within the *same paragraph*.
Links that fail this co-occurrence check are dropped.

Paragraphs (blank-line-separated chunks) replace the ±400-char window
the original plan proposed — in real digests rows sit 200-300 chars
apart, so a fixed-char window routinely spans neighbours and lets the
cross-row hallucination through. Paragraph boundaries follow the actual
structure of the digest.

Pre-LLM segmentation (split the email into spans before the model runs)
would be more thorough but reshapes the agent flow. This module is the
post-LLM safety net.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# How many tokens of the title to require in the paragraph. The title
# often gets suffixes like " | LinkedIn" or " - Acme Careers"; the first
# four significant tokens should appear together near the link.
_TITLE_TOKEN_PROBE = 4

# Minimum length for a significant token. Filters out stop-words like
# "a", "of", "to" without maintaining a stopword list.
_MIN_TOKEN_LEN = 3

_PARA_SPLIT = re.compile(r"\n\s*\n+")
_ROW_SEP = re.compile(r"\n[\s]*[-=*_~]{3,}[\s]*\n", re.MULTILINE)
_NON_WORD = re.compile(r"\W+")
_WS = re.compile(r"\s+")


def _normalize(s: str) -> str:
    return _WS.sub(" ", s.casefold()).strip()


def _significant_tokens(s: str, n: int) -> list[str]:
    return [t for t in _NON_WORD.split(s.casefold()) if len(t) >= _MIN_TOKEN_LEN][:n]


def _host_of(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _identifier_of(url: str) -> str | None:
    """Return a stable identifier for the URL — host for http(s),
    address for mailto. Used to gate ``_decide``: a URL with no
    identifier is malformed and gets dropped.

    Direct-solicitation emails (a recruiter writing "send your resume
    to hiring@acme.com") make the address itself the apply target, so
    the extractor emits `mailto:hiring@acme.com` as the job URL. That
    URL has no hostname — the address lives in the parsed path — so
    falling back on the path here keeps mailto URLs from being dropped
    while still letting row-anchoring catch cross-row hallucinations.
    """
    host = _host_of(url)
    if host:
        return host
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme == "mailto" and parsed.path:
        return parsed.path.lower()
    return None


def _paragraphs(text: str) -> list[str]:
    return [p for p in _PARA_SPLIT.split(text or "") if p.strip()]


def _rows(text: str) -> list[str]:
    """Split the email body into "rows" — one logical job entry each.

    Prefers separator lines (``----``, ``====``, etc.) when the digest
    uses them; otherwise falls back to blank-line paragraphs. Real
    digests vary, but any digest that mixes multiple jobs in one body
    needs SOME boundary; this picks up the common cases.
    """
    if not text:
        return []
    if _ROW_SEP.search(text):
        chunks = _ROW_SEP.split(text)
    else:
        chunks = _paragraphs(text)
    return [c for c in chunks if c.strip()]


def _signal_in(row: str, title_tokens: list[str], company: str) -> bool:
    norm = _normalize(row)
    if company and _normalize(company) in norm:
        return True
    if title_tokens:
        hits = sum(1 for t in title_tokens if t in norm)
        # Require all-but-one token to appear (forgiving of the LLM
        # rewording the title).
        if hits >= max(1, len(title_tokens) - 1):
            return True
    return False


def _find_url_rows(url: str, rows: list[str]) -> list[str]:
    """Return rows containing the URL (full first, then path-only)."""
    if not url or not rows:
        return []
    matches = [r for r in rows if url in r]
    if matches:
        return matches
    try:
        path = urlparse(url).path
    except ValueError:
        path = ""
    if path and len(path) >= 4:
        return [r for r in rows if path in r]
    return []


def _decide(link, email_text: str) -> tuple[bool, str]:
    url = (getattr(link, "url", "") or "").strip()
    if not url:
        return False, "no_url"
    if _identifier_of(url) is None:
        return False, "no_host"
    title_tokens = _significant_tokens(getattr(link, "title", "") or "", _TITLE_TOKEN_PROBE)
    company = (getattr(link, "company", "") or "").strip()
    if not title_tokens and not company:
        # Nothing to anchor against — extractor returned a bare URL.
        # Accept; the broader pipeline will catch missing-title cases.
        return True, ""
    rows = _rows(email_text)
    if len(rows) <= 1:
        # Single-row body — there is no cross-row swap to catch.
        return True, ""
    matching = _find_url_rows(url, rows)
    if not matching:
        return False, "url_absent"
    for row in matching:
        if _signal_in(row, title_tokens, company):
            return True, ""
    return False, "title_company_not_in_row"


def filter_span_atomic(links, email_text: str, *, email_id: str | None = None):
    """Drop ``JobLink`` records whose URL does not co-occur with the
    title or company text in any row of ``email_text``. Anchoring on
    URL (rather than host) handles same-host digests where every row
    has the same hostname but different paths — ZipRecruiter's ``/km/``
    tracker tokens, LinkedIn's ``/jobs/view/`` IDs. Logs each drop
    with structured fields so the operator can audit cross-row
    rejections.

    >>> from collections import namedtuple
    >>> Link = namedtuple("Link", "url title company")
    >>> body = (
    ...     "Acme Inc is hiring a Senior Backend Engineer. The role pays "
    ...     "well and the team is great. Apply at "
    ...     "https://jobs.acme.com/123\\n\\n"
    ...     "Separately, an unrelated vacancy at Beta Corp for a Frontend "
    ...     "Lead is at https://jobs.beta.com/456.")
    >>> good = Link("https://jobs.acme.com/123", "Senior Backend Engineer", "Acme Inc")
    >>> bad = Link("https://jobs.beta.com/456", "Senior Backend Engineer", "Acme Inc")
    >>> [k.url for k in filter_span_atomic([good, bad], body)]
    ['https://jobs.acme.com/123']
    """
    kept = []
    for link in links:
        ok, reason = _decide(link, email_text)
        if ok:
            kept.append(link)
            continue
        logger.warning(
            "span_validator drop: email_id=%s url=%s title=%r company=%r reason=%s",
            email_id,
            getattr(link, "url", ""),
            getattr(link, "title", ""),
            getattr(link, "company", ""),
            reason,
        )
    return kept
