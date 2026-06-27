"""Dependency-light HTML→markdown rendering for the email triage hot path.

Extracted from ``mcp_servers/email_clean.py`` so ``scripts/inbox_triage.py``
can render an html-only forward body without importing the MCP layer (which
pulls in ``fastmcp`` / ``logfire``). Keeps only ``html2text`` + ``bs4`` —
both base deps — and logs via stdlib ``logging`` instead of ``logfire`` to
stay out of the scripts hot path. ``mcp_servers/email_clean`` re-exports
``html_to_markdown`` from here so ``email_server`` is unchanged.
"""

from __future__ import annotations

import logging

import html2text
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


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
        logger.warning("Error converting HTML to markdown: %s", exc)
        return ""
