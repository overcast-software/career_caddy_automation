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


def _paragraphs(text: str) -> list[str]:
    return [p for p in _PARA_SPLIT.split(text or "") if p.strip()]


def _signal_in(paragraph: str, title_tokens: list[str], company: str) -> bool:
    norm = _normalize(paragraph)
    if company and _normalize(company) in norm:
        return True
    if title_tokens:
        hits = sum(1 for t in title_tokens if t in norm)
        # Require all-but-one token to appear (forgiving of the LLM
        # rewording the title).
        if hits >= max(1, len(title_tokens) - 1):
            return True
    return False


def _decide(link, email_text: str) -> tuple[bool, str]:
    host = _host_of(getattr(link, "url", ""))
    if not host:
        return False, "no_host"
    title_tokens = _significant_tokens(getattr(link, "title", "") or "", _TITLE_TOKEN_PROBE)
    company = (getattr(link, "company", "") or "").strip()
    if not title_tokens and not company:
        # Nothing to anchor against — extractor returned a bare URL.
        # Accept; the broader pipeline will catch missing-title cases.
        return True, ""
    paras = _paragraphs(email_text)
    if len(paras) <= 1:
        # Single-paragraph body — there is no cross-row swap to catch
        # because there are no rows. Accept and move on.
        return True, ""
    host_paras = [p for p in paras if host in p.casefold()]
    if not host_paras:
        return False, "host_absent"
    for para in host_paras:
        if _signal_in(para, title_tokens, company):
            return True, ""
    return False, "title_company_not_in_paragraph"


def filter_span_atomic(links, email_text: str, *, email_id: str | None = None):
    """Drop ``JobLink`` records whose host does not co-occur with the
    title or company text in any paragraph of ``email_text``. Logs each
    drop with structured fields so the operator can audit cross-row
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
