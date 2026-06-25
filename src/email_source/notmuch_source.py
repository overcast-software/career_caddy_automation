"""Notmuch implementation of :class:`EmailSource`.

Shells out to the ``notmuch`` CLI the same way
``scripts/tag_emails.py`` and ``scripts/process_tagged.py`` do. Nothing
fancy — just the subset the orchestrator needs.
"""

from __future__ import annotations

import email
import json
import logging
import subprocess
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import getaddresses

from src.email_source import EmailMeta, notmuch_folder_scope

logger = logging.getLogger(__name__)


def _decode_subject(raw: str) -> str:
    try:
        parts = decode_header(raw)
        return "".join(
            p.decode(c or "utf-8", "ignore") if isinstance(p, bytes) else p for p, c in parts
        )
    except Exception:
        return raw


# Compound query: an email needs processing if any stage is incomplete.
#   (NOT tag:evaluated)                                  — stage 1
#   (tag:job_post AND NOT tag:refined)                   — stage 2
#   (tag:follow_up AND NOT tag:caddy_processed)          — stage 3
_PENDING_QUERY = (
    "(NOT tag:evaluated) "
    "OR (tag:job_post AND NOT tag:refined) "
    "OR (tag:follow_up AND NOT tag:caddy_processed)"
)

# Envelope-recipient header preference. Forwarded job-board mail keeps the
# sender's original ``To:``, so the envelope headers (the address the message
# was actually *delivered* to) win over it. OQ-1 — exactly which header the
# catchall stamps — is confirmed at live mbsync acceptance (AUTO-23); this is
# the documented default preference order.
_RECIPIENT_HEADER_PREFERENCE = ("Delivered-To", "X-Original-To", "Envelope-To", "To")


def _scoped_query(base: str, days_back: int, folder: str | None) -> str:
    """Build a notmuch query from a base query, a date window, and an
    optional folder scope. Pure — no subprocess, no live index — so it is
    unit-testable directly.

    ``folder=None`` reproduces the legacy whole-index query (date-scoped
    only). A folder is AND-ed in as ``folder:"<name>"``; the value is quoted
    because ``@`` and ``.`` are notmuch/Xapian query special characters.
    The token resolves relative to the notmuch DB root (``NOTMUCH_MAILDIR``).
    """
    end = datetime.now()
    start = end - timedelta(days=days_back)
    date_range = f"date:{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"
    query = f"({base}) AND {date_range}"
    if folder:
        query = f'{query} AND folder:"{folder}"'
    return query


def _format_id_token(email_id: str) -> str:
    """notmuch ``id:`` term for an exact message-id match. The value is
    double-quoted so ``@`` / ``.`` are literals to Xapian, not operators."""
    raw = email_id[3:] if email_id.startswith("id:") else email_id
    return f'id:"{raw}"'


def _recipient_from_message(raw: str) -> str | None:
    """Extract the envelope recipient from a raw RFC-822 message.

    Pure (no subprocess / no live index): parses ``raw`` with the stdlib
    ``email`` module and returns the first address found while scanning
    ``_RECIPIENT_HEADER_PREFERENCE`` in order, or ``None``. A header may
    appear multiple times (each delivery hop prepends a ``Delivered-To``)
    and may carry multiple addresses; ``getaddresses`` flattens both.
    """
    msg = email.message_from_string(raw)
    for header in _RECIPIENT_HEADER_PREFERENCE:
        values = msg.get_all(header)
        if not values:
            continue
        addresses = [addr for _name, addr in getaddresses(values) if addr]
        if addresses:
            return addresses[0]
    return None


# Sentinel: ``NotmuchSource(folder=...)`` defaults to resolving from the env;
# pass an explicit folder (incl. ``None`` to force legacy whole-index) to override.
_FOLDER_FROM_ENV = object()


class NotmuchSource:
    """notmuch-backed :class:`EmailSource`.

    Scoped to a single maildir folder (the multi-user catchall) by default;
    see :func:`src.email_source.notmuch_folder_scope`. Pass ``folder`` to
    override env resolution (``None`` forces legacy whole-index queries).
    """

    def __init__(self, folder=_FOLDER_FROM_ENV) -> None:
        self.folder: str | None = notmuch_folder_scope() if folder is _FOLDER_FROM_ENV else folder

    async def list_pending(self, limit: int = 20, days_back: int = 14) -> list[EmailMeta]:
        query = _scoped_query(_PENDING_QUERY, days_back, self.folder)
        result = subprocess.run(
            ["notmuch", "search", "--format=json", f"--limit={limit}", query],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"notmuch search failed: {result.stderr.strip()}")
        threads = json.loads(result.stdout) if result.stdout.strip() else []
        out: list[EmailMeta] = []
        for thread in threads:
            query_arr = thread.get("query") or []
            if not query_arr or not query_arr[0]:
                continue
            raw_id = query_arr[0]
            if raw_id.startswith("id:"):
                raw_id = raw_id[3:]
            # query_arr[0] may be "msg1 id:msg2 ..." for multi-message threads;
            # take only the first message ID (used for content loading).
            raw_id = raw_id.split(" id:")[0]
            thread_id = thread.get("thread", "")
            out.append(
                EmailMeta(
                    id=raw_id,
                    subject=_decode_subject(thread.get("subject") or ""),
                    tags=set(thread.get("tags") or []),
                    thread_id=thread_id,
                )
            )
        return out

    async def add_tags(self, thread_id: str, tags: list[str]) -> None:
        if not tags:
            return
        args = [f"+{t}" for t in tags]
        subprocess.run(
            ["notmuch", "tag", *args, "--", f"thread:{thread_id}"],
            check=True,
            timeout=10,
        )

    async def get_recipient(self, email_id: str) -> str | None:
        """Return the envelope recipient (the catchall RCPT) for a message.

        Reads the raw message via ``notmuch show --format=raw`` and delegates
        header parsing to :func:`_recipient_from_message`, preferring the
        envelope recipient (Delivered-To / X-Original-To / Envelope-To) over
        the original ``To:``. AUTO-24 owns the ``@careercaddy.online`` →
        username resolution; this returns the raw address.

        Lazily called per message at the attribution call site, so it adds no
        subprocess to the ``list_pending`` hot path. Returns ``None`` when the
        message can't be read or carries no recognised recipient header.
        """
        result = subprocess.run(
            ["notmuch", "show", "--format=raw", _format_id_token(email_id)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning(
                "notmuch show --format=raw failed for id %s: %s",
                email_id,
                result.stderr.strip(),
            )
            return None
        if not result.stdout:
            return None
        return _recipient_from_message(result.stdout)

    async def count_by_query(self, query: str, days_back: int = 14) -> int:
        """Return thread count for an arbitrary notmuch query, scoped to
        the same date window + folder list_pending uses. Powers --status."""
        scoped = _scoped_query(query, days_back, self.folder)
        result = subprocess.run(
            ["notmuch", "count", "--output=threads", scoped],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"notmuch count failed: {result.stderr.strip()}")
        return int((result.stdout or "0").strip() or 0)

    async def list_by_query(
        self, query: str, limit: int = 20, days_back: int = 14
    ) -> list[EmailMeta]:
        """List EmailMetas matching an arbitrary query within the date
        window + folder scope. Used by --show <state>."""
        scoped = _scoped_query(query, days_back, self.folder)
        result = subprocess.run(
            ["notmuch", "search", "--format=json", f"--limit={limit}", scoped],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"notmuch search failed: {result.stderr.strip()}")
        threads = json.loads(result.stdout) if result.stdout.strip() else []
        out: list[EmailMeta] = []
        for thread in threads:
            query_arr = thread.get("query") or []
            if not query_arr or not query_arr[0]:
                continue
            raw_id = query_arr[0]
            if raw_id.startswith("id:"):
                raw_id = raw_id[3:]
            raw_id = raw_id.split(" id:")[0]
            thread_id = thread.get("thread", "")
            out.append(
                EmailMeta(
                    id=raw_id,
                    subject=_decode_subject(thread.get("subject") or ""),
                    tags=set(thread.get("tags") or []),
                    thread_id=thread_id,
                )
            )
        return out
