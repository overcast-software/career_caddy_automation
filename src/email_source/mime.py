"""Raw RFC-822 body extraction for the email triage hot path.

Ported from
``mcp_servers/email_server.EmailParser._extract_content_from_message`` so
``scripts/inbox_triage`` can pull the plain + html bodies out of a raw
message (``notmuch show --format=raw``) without importing the MCP layer.

KEEPS the ``message/rfc822`` recursion so a "forward as attachment" — the
nested original wrapped inside Doug's forward — is unwrapped rather than
skipped. Dependency-light: stdlib ``email`` only, ``logging`` (not
``logfire``).
"""

from __future__ import annotations

import email
import logging
from email.message import Message

logger = logging.getLogger(__name__)


def _walk(msg: Message, plain_text: str, html_content: str) -> tuple[str, str]:
    """Recursively accumulate text/plain + text/html content from a message,
    descending into nested ``message/rfc822`` (forwarded) parts."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            # Nested message/rfc822 (forward-as-attachment): unwrap the
            # encapsulated original and recurse.
            if content_type == "message/rfc822":
                payload = part.get_payload()
                if payload and isinstance(payload, list) and len(payload) > 0:
                    nested_msg = payload[0]
                    plain_text, html_content = _walk(nested_msg, plain_text, html_content)
            elif content_type == "text/plain":
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
    ``("", "<html>…")``. Nested ``message/rfc822`` parts are unwrapped so
    forward-as-attachment content is captured.
    """
    msg = email.message_from_string(raw)
    return _walk(msg, "", "")
