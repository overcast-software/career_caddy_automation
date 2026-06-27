"""Shared HTML/markdown cleaning for email MCP servers.

Both the notmuch-backed ``email_server`` and the IMAP-backed ``imap_server``
feed LLMs, and both pay for every token. This module centralises the
"prepare email body for classification" pipeline so both backends behave
identically.
"""

from __future__ import annotations

import re

# ``html_to_markdown`` moved to ``src/email_source/html_render`` so the email
# triage scripts can render bodies without importing this MCP module (and its
# ``fastmcp``/``logfire`` weight). Re-exported here so ``email_server`` \u2014
# which does ``from mcp_servers.email_clean import clean_markdown,
# html_to_markdown`` \u2014 is unchanged.
from src.email_source.html_render import html_to_markdown

__all__ = ["clean_markdown", "html_to_markdown"]

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
