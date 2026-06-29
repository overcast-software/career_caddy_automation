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
from email.utils import getaddresses

logger = logging.getLogger(__name__)

_CADDY_DOMAIN = "@careercaddy.online"
# Recipient headers scanned in priority order. Delivered-To / X-Original-To
# reflect the actual envelope drop (the catchall's true target) and win over a
# cosmetic To when both are present; To is the fallback the genuine per-user
# forwards carry on their own (verified live: ``dough@`` catchall mail has only
# a To header).
_RECIPIENT_HEADERS = ("Delivered-To", "X-Original-To", "To")


def extract_recipient(raw: str) -> str | None:
    """Return the ``@careercaddy.online`` localpart this message was addressed to.

    Scans the recipient headers in priority ``Delivered-To`` > ``X-Original-To``
    > ``To`` and returns the localpart of the first ``@careercaddy.online``
    address found — e.g. ``"dough"`` for ``dough@careercaddy.online``. This is
    the owner-resolution key for the catchall hard gate (AUTO-18 M1).

    Returns ``None`` when no ``@careercaddy.online`` recipient is present. The
    catchall maildir over-captures original job-board alerts addressed to the
    operator's personal aliases (``doug@passiveobserver.com`` etc.); those have
    no CC owner and must drop without a JobPost. ``getaddresses`` handles both
    bare (``dough@careercaddy.online``) and display-name
    (``"Dough" <dough@careercaddy.online>``) forms.
    """
    msg = email.message_from_string(raw)
    for header in _RECIPIENT_HEADERS:
        values = msg.get_all(header, [])
        for _name, addr in getaddresses(values):
            addr = (addr or "").strip().lower()
            if addr.endswith(_CADDY_DOMAIN):
                return addr[: -len(_CADDY_DOMAIN)] or None
    return None


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
