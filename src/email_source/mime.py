"""Raw RFC-822 body extraction for the email triage hot path.

Ported from
``mcp_servers/email_server.EmailParser._extract_content_from_message`` so
``scripts/inbox_triage`` can pull the plain + html bodies out of a raw
message (``notmuch show --format=raw``) without importing the MCP layer.

Handles "forward as attachment" — the nested original wrapped inside
Doug's forward — because ``Message.walk()`` already descends into
``message/rfc822`` subparts (their payload is a list, so ``is_multipart()``
is True and ``walk()`` recurses), so the nested ``text/plain`` / ``text/html``
parts are captured by the normal branches. Dependency-light: stdlib
``email`` only, ``logging`` (not ``logfire``).
"""

from __future__ import annotations

import email
import logging
from email.message import Message

logger = logging.getLogger(__name__)


def _walk(msg: Message, plain_text: str, html_content: str) -> tuple[str, str]:
    """Accumulate text/plain + text/html content from a message.

    ``msg.walk()`` already descends into nested ``message/rfc822``
    (forward-as-attachment) parts, so the encapsulated original's
    ``text/plain`` / ``text/html`` are captured by the branches below
    exactly once — no explicit recursion needed (adding it double-counts)."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        plain_text += payload.decode("utf-8", errors="ignore") + "\n"
                    except Exception as exc:
                        logger.warning("Error decoding plain text: %s", exc)
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        html_content += payload.decode("utf-8", errors="ignore") + "\n"
                    except Exception as exc:
                        logger.warning("Error decoding HTML content: %s", exc)
    else:
        # Single-part message.
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                decoded_content = payload.decode("utf-8", errors="ignore")
                if content_type == "text/html":
                    html_content += decoded_content + "\n"
                else:
                    plain_text += decoded_content + "\n"
            except Exception as exc:
                logger.warning("Error decoding single part content: %s", exc)
    return plain_text, html_content


def extract_bodies(raw: str) -> tuple[str, str]:
    """Return ``(plain_text, html)`` for a raw RFC-822 message string.

    Either half may be empty: an html-only Thunderbird forward yields
    ``("", "<html>…")``. Nested ``message/rfc822`` parts are captured via
    ``Message.walk()``'s own descent so forward-as-attachment content is
    included.
    """
    msg = email.message_from_string(raw)
    return _walk(msg, "", "")
