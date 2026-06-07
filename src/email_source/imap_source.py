"""IMAP implementation of :class:`EmailSource` + catchall-poller IMAP client.

This module hosts two distinct surfaces:

1. ``ImapSource`` — the :class:`EmailSource` Protocol implementation
   the inbox-triage daemon would use if an operator chose
   ``CADDY_EMAIL_BACKEND=imap``. The tag-emulation layer (notmuch-style
   "evaluated/refined/follow_up/caddy_processed" markers backed by a
   local store) is *not yet implemented*; ``list_pending`` and
   ``add_tags`` raise :class:`NotImplementedError` so the inbox-triage
   path fails fast with a clear message instead of silently no-oping.
   See ``notes.org`` → Phase D for the design sketch.

2. ``CatchallImapClient`` — the simpler surface the B3 catchall poller
   uses. The catchall mailbox doesn't need tag emulation: the poller
   reads unseen messages, processes them, and marks them seen (or
   moves them to a Processed folder). No notmuch-style tag overlay.

Library: ``aioimaplib`` (declared in the ``imap`` optional-dependency
group). The client is sync-callable by way of asyncio.run() at the
poller entry point; aioimaplib's awaitables are wrapped here.
"""

from __future__ import annotations

import email
import email.policy
import logging
import os
from dataclasses import dataclass, field
from email.message import EmailMessage

from src.email_source import EmailMeta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EmailSource Protocol stub (inbox-triage path, IMAP backend not implemented).
# ---------------------------------------------------------------------------


class ImapSource:
    async def list_pending(self, limit: int = 20, days_back: int = 14) -> list[EmailMeta]:
        raise NotImplementedError(
            "IMAP backend not yet implemented for inbox_triage. "
            "See notes.org Phase D and the catchall poller's CatchallImapClient "
            "for the B3-shaped IMAP surface. Set CADDY_EMAIL_BACKEND=notmuch for "
            "the inbox-triage daemon."
        )

    async def add_tags(self, thread_id: str, tags: list[str]) -> None:
        raise NotImplementedError(
            "IMAP backend not yet implemented for inbox_triage. "
            "Set CADDY_EMAIL_BACKEND=notmuch for the inbox-triage daemon."
        )


# ---------------------------------------------------------------------------
# Catchall poller — B3 (per notes.org/Roadmap/Phase B).
# ---------------------------------------------------------------------------


@dataclass
class CatchallMessage:
    """One catchall mail the poller will process.

    The poller resolves the *first* RCPT TO localpart that matches
    ``<localpart>@<CADDY_CATCHALL_DOMAIN>`` (typically
    ``careercaddy.online``) — recipients on other domains, when present,
    are ignored. ``uid`` is the IMAP UID within ``mailbox``; it's used
    to mark the message seen / processed on the server side.
    """

    uid: str
    message_id: str
    subject: str
    sender: str
    to_addresses: list[str]
    body_text: str
    forwarded_to_localpart: str | None
    forwarded_via_address: str | None
    raw_size: int
    mailbox: str = "INBOX"
    extras: dict = field(default_factory=dict)


def _parse_recipient_addresses(msg: EmailMessage) -> list[str]:
    """Pull all RCPT-relevant addresses out of an email.message.EmailMessage.

    We look at To: + Cc: + Delivered-To: + X-Original-To:. The catchall
    MTA SHOULD propagate the original RCPT TO via Delivered-To or
    X-Original-To when forwarding hits the actual catchall mailbox
    (Maddy does this by default); without that header we fall back to
    the To: line, which is what the user typed.
    """
    out: list[str] = []
    for header in ("Delivered-To", "X-Original-To", "To", "Cc"):
        for value in msg.get_all(header) or []:
            for addr in email.utils.getaddresses([value]):
                cleaned = (addr[1] or "").strip().lower()
                if cleaned and cleaned not in out:
                    out.append(cleaned)
    return out


def _resolve_forward_localpart(
    addresses: list[str], catchall_domain: str
) -> tuple[str | None, str | None]:
    """Return ``(localpart, forwarded_via_address)`` for the first address
    matching ``<localpart>@<catchall_domain>``, case-folded.

    Returns ``(None, None)`` if no address matches the catchall domain
    (the message will be skipped / bounced upstream of the poller).
    """
    catchall_domain = catchall_domain.lower().strip()
    if not catchall_domain:
        return None, None
    for addr in addresses:
        if "@" not in addr:
            continue
        local, _, domain = addr.partition("@")
        if domain.strip().lower() == catchall_domain:
            local = local.strip()
            if local:
                return local, f"{local}@{catchall_domain}"
    return None, None


def _body_text(msg: EmailMessage) -> str:
    """Best-effort plain-text body extraction. Falls back to text/html
    when no *substantive* text/plain part exists; the catchall poller's
    downstream URL-extractor agent handles HTML stripping at the LLM
    layer.

    "Substantive" check: the plain part is preferred only when it has
    non-whitespace content. Some senders ship an empty text/plain part
    alongside the real HTML body (Marketo / HubSpot / many CRMs do
    this); falling back to HTML in that case avoids handing the
    extractor an empty string.
    """
    if msg.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    plain_parts.append(part.get_content())
                except Exception:
                    pass
            elif ctype == "text/html":
                try:
                    html_parts.append(part.get_content())
                except Exception:
                    pass
        substantive_plain = [p for p in plain_parts if p and p.strip()]
        if substantive_plain:
            return "\n\n".join(substantive_plain)
        if html_parts:
            return "\n\n".join(html_parts)
        return ""
    try:
        return msg.get_content() or ""
    except Exception:
        return ""


def parse_catchall_message(
    raw_bytes: bytes,
    uid: str,
    catchall_domain: str,
    mailbox: str = "INBOX",
) -> CatchallMessage:
    """Parse one raw RFC-5322 message into a :class:`CatchallMessage`.

    Exposed at module scope so tests can construct ``raw_bytes`` directly
    without needing a live IMAP server.
    """
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    addresses = _parse_recipient_addresses(msg)
    localpart, via = _resolve_forward_localpart(addresses, catchall_domain)
    return CatchallMessage(
        uid=uid,
        message_id=(msg.get("Message-Id") or "").strip(),
        subject=str(msg.get("Subject") or ""),
        sender=str(msg.get("From") or "").strip(),
        to_addresses=addresses,
        body_text=_body_text(msg),
        forwarded_to_localpart=localpart,
        forwarded_via_address=via,
        raw_size=len(raw_bytes),
        mailbox=mailbox,
    )


class CatchallImapClient:
    """Async IMAP client for the catchall mailbox.

    Configured via env (no positional secrets in code):

    - ``CADDY_CATCHALL_IMAP_HOST`` — IMAP server hostname.
    - ``CADDY_CATCHALL_IMAP_PORT`` — TCP port (default 993 IMAP-SSL).
    - ``CADDY_CATCHALL_IMAP_USER`` — login username.
    - ``CADDY_CATCHALL_IMAP_PASS`` — login password.
    - ``CADDY_CATCHALL_IMAP_MAILBOX`` — folder to read (default ``INBOX``).
    - ``CADDY_CATCHALL_DOMAIN`` — the catchall domain (e.g.
      ``careercaddy.online``); the poller uses this to pull the
      forwarded-to localpart out of each message.

    Methods are async because ``aioimaplib`` is async-native.

    Lifecycle: ``async with client:`` opens / logs in / SELECTs and
    closes / logs out. Reuse one client across many ``fetch_unseen``
    calls.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        mailbox: str | None = None,
        catchall_domain: str | None = None,
        use_ssl: bool = True,
    ) -> None:
        self.host = host or os.environ["CADDY_CATCHALL_IMAP_HOST"]
        self.port = port or int(os.environ.get("CADDY_CATCHALL_IMAP_PORT", "993"))
        self.user = user or os.environ["CADDY_CATCHALL_IMAP_USER"]
        self.password = password or os.environ["CADDY_CATCHALL_IMAP_PASS"]
        self.mailbox = mailbox or os.environ.get("CADDY_CATCHALL_IMAP_MAILBOX", "INBOX")
        self.catchall_domain = catchall_domain or os.environ.get(
            "CADDY_CATCHALL_DOMAIN", "careercaddy.online"
        )
        self.use_ssl = use_ssl
        self._imap = None  # aioimaplib.IMAP4_SSL once connected

    async def __aenter__(self) -> CatchallImapClient:
        from aioimaplib import aioimaplib

        if self.use_ssl:
            self._imap = aioimaplib.IMAP4_SSL(host=self.host, port=self.port)
        else:
            self._imap = aioimaplib.IMAP4(host=self.host, port=self.port)
        await self._imap.wait_hello_from_server()
        login_resp = await self._imap.login(self.user, self.password)
        if login_resp.result != "OK":
            raise RuntimeError(f"IMAP login failed: {login_resp.result} {login_resp.lines!r}")
        select_resp = await self._imap.select(self.mailbox)
        if select_resp.result != "OK":
            raise RuntimeError(
                f"IMAP SELECT {self.mailbox!r} failed: {select_resp.result} {select_resp.lines!r}"
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._imap is None:
            return
        try:
            await self._imap.logout()
        except Exception as exc:
            logger.debug("IMAP logout raised (ignored): %s", exc)
        self._imap = None

    async def fetch_unseen(self, limit: int = 20) -> list[CatchallMessage]:
        """Pull up to ``limit`` UNSEEN messages, parsed. Does NOT mark
        them seen — call :meth:`mark_processed` after a successful
        downstream POST so a crash mid-flight surfaces the message again
        on the next poll instead of getting eaten.
        """
        if self._imap is None:
            raise RuntimeError("CatchallImapClient used outside of `async with`")
        search_resp = await self._imap.search("UNSEEN")
        if search_resp.result != "OK":
            raise RuntimeError(
                f"IMAP SEARCH UNSEEN failed: {search_resp.result} {search_resp.lines!r}"
            )
        uids: list[str] = []
        for line in search_resp.lines:
            if isinstance(line, bytes):
                line = line.decode("ascii", errors="ignore")
            uids.extend(tok for tok in line.split() if tok.isdigit())
        uids = uids[:limit]
        messages: list[CatchallMessage] = []
        for uid in uids:
            fetch_resp = await self._imap.fetch(uid, "(RFC822)")
            if fetch_resp.result != "OK":
                logger.warning("IMAP FETCH %s failed: %s", uid, fetch_resp.result)
                continue
            raw_bytes = b""
            for line in fetch_resp.lines:
                if isinstance(line, (bytes, bytearray)):
                    raw_bytes += bytes(line)
            if not raw_bytes:
                logger.warning("IMAP FETCH %s returned empty body", uid)
                continue
            try:
                messages.append(
                    parse_catchall_message(
                        raw_bytes,
                        uid=uid,
                        catchall_domain=self.catchall_domain,
                        mailbox=self.mailbox,
                    )
                )
            except Exception as exc:
                logger.warning("failed to parse catchall uid=%s: %s", uid, exc)
        return messages

    async def mark_processed(self, uid: str) -> None:
        """Mark a message ``\\Seen`` after it's been successfully
        processed. The poller's contract: only call this AFTER the
        JobPost POST has landed (or after a deliberate bounce). On
        crash mid-flight the message stays UNSEEN and the next poll
        picks it up again.
        """
        if self._imap is None:
            raise RuntimeError("CatchallImapClient used outside of `async with`")
        store_resp = await self._imap.store(uid, "+FLAGS", "\\Seen")
        if store_resp.result != "OK":
            logger.warning(
                "IMAP STORE +Seen on uid=%s failed: %s %r",
                uid,
                store_resp.result,
                store_resp.lines,
            )

    async def expunge(self) -> None:
        """Optional cleanup; the poller doesn't need it unless the
        operator wants to actually delete processed mail."""
        if self._imap is None:
            return
        await self._imap.expunge()
