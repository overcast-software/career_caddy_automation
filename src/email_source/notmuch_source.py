"""Notmuch implementation of :class:`EmailSource`.

Shells out to the ``notmuch`` CLI the same way
``scripts/tag_emails.py`` and ``scripts/process_tagged.py`` do. Nothing
fancy — just the subset the orchestrator needs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from email.header import decode_header

from src.email_source import EmailMeta
from src.email_source.mime import extract_recipient

logger = logging.getLogger(__name__)


def _decode_subject(raw: str) -> str:
    try:
        parts = decode_header(raw)
        return "".join(
            p.decode(c or "utf-8", "ignore") if isinstance(p, bytes) else p for p, c in parts
        )
    except Exception:
        return raw


def _message_tags(message_id: str) -> set[str]:
    """Return the OWN tags of a single message, by id.

    notmuch ``search`` summaries report the **thread-union** tags, which
    poison a freshly-forwarded job that shares a thread with an
    already-processed sibling (AUTO-32): the forward inherits the
    original alert's ``evaluated``/``caddy_processed`` and the
    orchestrator short-circuits it to ``already_done``. Routing
    decisions must read the matched message's own tags, so we resolve
    them per-message. ``--output=tags id:<msgid>`` matches exactly one
    message, so the union it returns IS that message's own tag set.
    """
    result = subprocess.run(
        ["notmuch", "search", "--output=tags", f"id:{message_id}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch search tags failed: {result.stderr.strip()}")
    return {t.strip() for t in (result.stdout or "").splitlines() if t.strip()}


def _message_recipient(message_id: str) -> str | None:
    """Return the ``@careercaddy.online`` recipient localpart of one message.

    Pulls the raw message (same per-message pattern as ``_message_tags``) and
    runs ``extract_recipient`` over its headers. ``None`` on any failure or
    when the message carries no ``@careercaddy.online`` recipient — the catchall
    hard gate treats that as "no owner" and drops the message before any LLM
    call (AUTO-18 M1). Best-effort: a notmuch error must not abort listing.
    """
    try:
        result = subprocess.run(
            ["notmuch", "show", "--format=raw", f"id:{message_id}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        logger.debug("recipient read failed for %s (non-fatal)", message_id, exc_info=True)
        return None
    if result.returncode != 0:
        return None
    return extract_recipient(result.stdout or "")


def _matched_message_id(thread: dict) -> str | None:
    """Extract the matched message id from a notmuch thread summary.

    ``thread["query"][0]`` is the query for the messages that matched the
    search (vs. ``[1]`` for the unmatched siblings). For a forward sharing
    a thread with a processed original, only the forward matches the
    pending query, so this is the forward's id — the message we must route
    and tag. May be ``"id:a id:b ..."`` for multi-message matches; take the
    first.
    """
    query_arr = thread.get("query") or []
    if not query_arr or not query_arr[0]:
        return None
    raw_id = query_arr[0]
    if raw_id.startswith("id:"):
        raw_id = raw_id[3:]
    # query_arr[0] may be "msg1 id:msg2 ..." for multi-message matches;
    # take only the first message ID (used for content loading + tagging).
    return raw_id.split(" id:")[0]


def _thread_to_meta(thread: dict) -> EmailMeta | None:
    """Build an EmailMeta for the matched message of a thread summary.

    Tags come from the matched message's OWN tags (``_message_tags``), not
    the thread union, so a new forward isn't read as done because of a
    processed sibling. ``thread_id`` is kept for content-load context.
    """
    raw_id = _matched_message_id(thread)
    if not raw_id:
        return None
    return EmailMeta(
        id=raw_id,
        subject=_decode_subject(thread.get("subject") or ""),
        tags=_message_tags(raw_id),
        thread_id=thread.get("thread", ""),
        recipient=_message_recipient(raw_id),
    )


# Catchall-maildir selector (AUTO-18 M1). The per-user catchall delivers every
# ``<username>@careercaddy.online`` message into one maildir folder, so we sweep
# the WHOLE folder by ``path:`` and let the triage loop's hard gate decide
# ownership per message (the recipient localpart must resolve to a CC user).
#
# Why path:, not to: — validated live against the maildir: a
# ``to:"forwarding@careercaddy.online"`` selector matches only ~194 messages
# (the handful Doug forwarded to that bare address) and STARVES the real
# per-user catchall, while ``path:forwarding@careercaddy.online/Inbox/**``
# matches the full folder (~2.6k) — so ``dough@``/``wisevehicle@`` mail is now
# captured. The folder over-captures original job-board alerts delivered to the
# operator's personal aliases (``doug@passiveobserver.com`` etc.); those carry
# no ``@careercaddy.online`` recipient, so the ``_triage_one`` no-user gate
# drops them cheaply before any LLM call. ``caddy_processed`` is the single
# terminal tag the triage loop writes on every exit path, so
# ``NOT tag:caddy_processed`` is the real pending working set.
#
# Folder is the maildir-relative path under the notmuch database root; override
# with ``CADDY_INBOX_NOTMUCH_FOLDER``. The ``path:`` term is passed as ONE
# notmuch argv element (no shell), so the ``@`` and ``**`` glob are taken
# literally by notmuch, never the shell.
_CATCHALL_FOLDER = os.environ.get(
    "CADDY_INBOX_NOTMUCH_FOLDER", "forwarding@careercaddy.online/Inbox"
)
_PENDING_QUERY = f"path:{_CATCHALL_FOLDER}/** AND NOT tag:caddy_processed"


class NotmuchSource:
    async def list_pending(self, limit: int = 20, days_back: int = 14) -> list[EmailMeta]:
        end = datetime.now()
        start = end - timedelta(days=days_back)
        date_range = f"date:{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"
        query = f"({_PENDING_QUERY}) AND {date_range}"
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
            meta = _thread_to_meta(thread)
            if meta is not None:
                out.append(meta)
        return out

    async def add_tags(self, message_id: str, tags: list[str]) -> None:
        """Tag a single MESSAGE by id, never its whole thread.

        Tagging ``thread:{id}`` (the pre-AUTO-32 behavior) stamps a processed
        message's tags onto its not-yet-processed siblings — e.g. processing
        an original ZipRecruiter alert poisons the forward in the same thread
        with ``evaluated``/``caddy_processed`` so it's never triaged. The
        same-job double-post guard belongs to JobPost dedupe (canonical_link),
        not thread-tag skipping.
        """
        if not tags:
            return
        args = [f"+{t}" for t in tags]
        subprocess.run(
            ["notmuch", "tag", *args, "--", f"id:{message_id}"],
            check=True,
            timeout=10,
        )

    @staticmethod
    def _date_scoped(query: str, days_back: int) -> str:
        end = datetime.now()
        start = end - timedelta(days=days_back)
        date_range = f"date:{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"
        return f"({query}) AND {date_range}"

    async def count_by_query(self, query: str, days_back: int = 14) -> int:
        """Return thread count for an arbitrary notmuch query, scoped to
        the same date window list_pending uses. Powers --status."""
        scoped = self._date_scoped(query, days_back)
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
        window. Used by --show <state>."""
        scoped = self._date_scoped(query, days_back)
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
            meta = _thread_to_meta(thread)
            if meta is not None:
                out.append(meta)
        return out

    async def list_by_message_id(self, message_id: str) -> list[EmailMeta]:
        """Fetch a single message by its notmuch Message-ID — NOT date-scoped.

        Powers ``caddy-inbox --message-id`` for one-off targeted (re)triage of
        a specific forward (debugging, or the AUTO-36 extraction-eval cases).
        Unlike ``list_pending`` / ``list_by_query`` there is no ``date:``
        window: a Message-ID is globally unique, so date-scoping would only
        risk missing an older message. Returns ``[]`` when nothing matches.
        Tag state is untouched — an already-processed message still triages to
        ``already_done`` downstream. Accepts a bare id, an ``id:``-prefixed id,
        or an ``<angle-bracketed>`` id.
        """
        mid = message_id.strip().removeprefix("id:").strip().strip("<>")
        result = subprocess.run(
            ["notmuch", "search", "--format=json", "--limit=1", f"id:{mid}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"notmuch search failed: {result.stderr.strip()}")
        threads = json.loads(result.stdout) if result.stdout.strip() else []
        out: list[EmailMeta] = []
        for thread in threads:
            meta = _thread_to_meta(thread)
            if meta is not None:
                out.append(meta)
        return out
