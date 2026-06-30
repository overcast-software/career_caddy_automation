"""IMAP implementation of :class:`EmailSource`.

``ImapSource`` is the :class:`EmailSource` Protocol implementation the
inbox-triage daemon would use if an operator chose
``CADDY_EMAIL_BACKEND=imap``. The tag-emulation layer (notmuch-style
"evaluated/refined/follow_up/caddy_processed" markers backed by a local
store) is *not yet implemented*; every method raises
:class:`NotImplementedError` so the inbox-triage path fails fast with a
clear message instead of silently no-oping. See ``notes.org`` → Phase D
for the design sketch.

To implement:
  * back the "tag" concept with a local store keyed on
    ``(account, folder, uidvalidity, uid, tag)``;
  * use ``aioimaplib`` for async IMAP I/O (declared in the ``imap``
    optional-dependency group);
  * expose a matching MCP server (``mcp_servers/imap_server.py``) with the
    same tool surface as ``email_server.py`` so the existing agents work
    without branching on backend.

Until that lands, ``make_source(backend='imap')`` and
``caddy-inbox --backend imap`` fail fast with a clear message.
"""

from __future__ import annotations

from src.email_source import EmailMeta


class ImapSource:
    async def list_pending(self, limit: int = 20, days_back: int = 14) -> list[EmailMeta]:
        raise NotImplementedError(
            "IMAP backend not yet implemented for inbox_triage. "
            "See notes.org Phase D for the design. "
            "Set CADDY_EMAIL_BACKEND=notmuch for the inbox-triage daemon."
        )

    async def add_tags(self, message_id: str, tags: list[str]) -> None:
        raise NotImplementedError(
            "IMAP backend not yet implemented for inbox_triage. "
            "Set CADDY_EMAIL_BACKEND=notmuch for the inbox-triage daemon."
        )

    async def count_by_query(self, query: str, days_back: int = 14) -> int:
        raise NotImplementedError(
            "IMAP backend has no notmuch-style tag query support yet; "
            "--status only works with CADDY_EMAIL_BACKEND=notmuch."
        )

    async def list_by_query(
        self, query: str, limit: int = 20, days_back: int = 14
    ) -> list[EmailMeta]:
        raise NotImplementedError(
            "IMAP backend has no notmuch-style tag query support yet; "
            "--show only works with CADDY_EMAIL_BACKEND=notmuch."
        )

    async def list_by_message_id(self, message_id: str) -> list[EmailMeta]:
        raise NotImplementedError(
            "IMAP backend has no message-id fetch support yet; "
            "--message-id only works with CADDY_EMAIL_BACKEND=notmuch."
        )
