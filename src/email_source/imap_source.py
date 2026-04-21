"""IMAP implementation of :class:`EmailSource`.

**Not yet implemented.** The full design lives in ``notes.org:426`` and the
approved plan at ``/home/oldbones/.claude/plans/resilient-beaming-clarke.md``.

To implement:
  * back the "tag" concept with a sqlite store keyed on
    ``(account, folder, uidvalidity, uid, tag)``;
  * use ``aioimaplib`` for async IMAP I/O;
  * expose a matching MCP server (``mcp_servers/imap_server.py``) with the
    same tool surface as ``email_server.py`` so the existing agents work
    without branching on backend.

Until that lands, ``make_source(backend='imap')`` will raise and
``caddy-inbox --backend imap`` will fail fast with a clear message.
"""

from __future__ import annotations

from src.email_source import EmailMeta


class ImapSource:
    async def list_pending(self, limit: int = 20, days_back: int = 14) -> list[EmailMeta]:
        raise NotImplementedError(
            "IMAP backend not yet implemented. "
            "See notes.org:426 and the caddy-inbox plan for the design. "
            "Set CADDY_EMAIL_BACKEND=notmuch for now."
        )

    async def add_tags(self, email_id: str, tags: list[str]) -> None:
        raise NotImplementedError(
            "IMAP backend not yet implemented. Set CADDY_EMAIL_BACKEND=notmuch for now."
        )
