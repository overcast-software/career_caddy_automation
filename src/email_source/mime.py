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
import os
from email.message import Message
from email.utils import getaddresses

logger = logging.getLogger(__name__)

_CADDY_DOMAIN = "@careercaddy.online"
# The catchall *sink* localpart. purelymail's catchall rewrites every
# ``*@careercaddy.online`` envelope onto one mailbox and stamps
# ``Delivered-To: forwarding@careercaddy.online`` on the way in — so the
# envelope headers read ``forwarding`` no matter who the sender actually
# addressed. It is the sink, never a real CC user: owner resolution must skip
# it and fall through to the genuine ``<username>@`` recipient that the
# original ``To`` carries (verified live: ``Delivered-To: forwarding@`` +
# ``To: wisevehicle@`` must resolve to ``wisevehicle``, not ``forwarding``).
# Override to match a different catchall mailbox via ``CADDY_CATCHALL_LOCALPART``.
_CATCHALL_LOCALPART = os.environ.get("CADDY_CATCHALL_LOCALPART", "forwarding").strip().lower()
# Recipient headers scanned in priority order. Delivered-To / X-Original-To
# reflect the envelope drop and outrank a cosmetic To for self-hosters whose
# MTA stamps the real per-user target there; To is what this catchall's genuine
# per-user forwards carry. The first NON-sink ``@careercaddy.online`` recipient
# in this order wins.
_RECIPIENT_HEADERS = ("Delivered-To", "X-Original-To", "To")


def extract_recipient(raw: str) -> str | None:
    """Return the ``@careercaddy.online`` localpart this message was addressed to.

    Scans the recipient headers in priority ``Delivered-To`` > ``X-Original-To``
    > ``To`` and returns the localpart of the first ``@careercaddy.online``
    address found — e.g. ``"dough"`` for ``dough@careercaddy.online`` — *skipping
    the catchall sink* (:data:`_CATCHALL_LOCALPART`, default ``forwarding``). The
    catchall stamps ``Delivered-To: forwarding@`` on every message, so without
    the skip every forward would resolve to the sink and never to the user who
    was actually addressed. This is the owner-resolution key for the catchall
    hard gate (AUTO-18 M1).

    Returns ``None`` when no genuine ``@careercaddy.online`` recipient is present
    — either no caddy address at all (an over-captured personal-alias original
    like ``doug@passiveobserver.com``), or *only* the catchall sink
    (``forwarding@`` with no per-user ``To``). Both have no CC owner and must
    drop without a JobPost. ``getaddresses`` handles both bare
    (``dough@careercaddy.online``) and display-name
    (``"Dough" <dough@careercaddy.online>``) forms.
    """
    msg = email.message_from_string(raw)
    for header in _RECIPIENT_HEADERS:
        values = msg.get_all(header, [])
        for _name, addr in getaddresses(values):
            addr = (addr or "").strip().lower()
            if not addr.endswith(_CADDY_DOMAIN):
                continue
            localpart = addr[: -len(_CADDY_DOMAIN)]
            if not localpart or localpart == _CATCHALL_LOCALPART:
                # The catchall sink — every forward lands here regardless of who
                # it was addressed to. Skip it and keep scanning for the genuine
                # per-user recipient on a later header / address.
                continue
            return localpart
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
