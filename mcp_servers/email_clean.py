"""Shared HTML/markdown cleaning for email MCP servers.

Both the notmuch-backed ``email_server`` and the IMAP-backed ``imap_server``
feed LLMs, and both pay for every token. This module centralises the
"prepare email body for classification" pipeline so both backends behave
identically.
"""

from __future__ import annotations

import re

import html2text
import logfire
from bs4 import BeautifulSoup

_ZW_CHARS = re.compile(r"[\u034f\u200b\u200c\u200d\u00ad\u2060\ufeff]")
_TRIPLE_BLANK = re.compile(r"\n\s*\n\s*\n+")
_TRACKING_IMG = re.compile(r"!\[\]\([^)]*\.(?:gif|png)[^)]*\)")
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(\s*[^)]*\)", re.DOTALL)
_BARE_URL = re.compile(r"https?://\S+")
_HRULE = re.compile(r"(?m)^[-|*_\s]{3,}$")
_PIPE_ONLY = re.compile(r"(?m)^[\s|]+$")
_FOOTER = re.compile(
    r"(?mi)^.*(unsubscribe|manage (your )?preferences|"
    r"this (email|message) was sent|view (this|it) (in|online)|"
    r"update your preferences|\xa9\s*20\d\d|all rights reserved|"
    r"privacy policy|terms of (use|service)).*$"
)


def clean_markdown(md: str, classify: bool = False) -> str:
    """Strip noise from HTML-derived markdown.

    classify=True is lossy — drops URLs/images/footer boilerplate. Don't use
    when downstream needs links (e.g. url extraction).
    """
    md = _ZW_CHARS.sub("", md)
    md = _TRACKING_IMG.sub("", md)
    if classify:
        md = _IMG.sub("", md)
        md = _MD_LINK.sub(r"\1", md)
        md = _BARE_URL.sub("", md)
        md = _FOOTER.sub("", md)
        md = _HRULE.sub("", md)
        md = _PIPE_ONLY.sub("", md)
    md = _TRIPLE_BLANK.sub("\n\n", md)
    return md.strip()


def html_to_markdown(html: str) -> str:
    """Convert raw HTML to markdown via BeautifulSoup + html2text.

    Returns an empty string and logs on conversion failure so callers can
    fall back to plain text without branching on exceptions.
    """
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0
    h.unicode_snob = True
    h.ignore_emphasis = False
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return h.handle(str(soup))
    except Exception as exc:
        logfire.warning(f"Error converting HTML to markdown: {exc}")
        return ""
