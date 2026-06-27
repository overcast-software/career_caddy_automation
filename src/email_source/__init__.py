"""Backend-agnostic email source for the triage pipeline.

The MCP servers (``mcp_servers/email_server.py``, ``mcp_servers/imap_server.py``)
expose identical tool names to *agents*. The orchestrator itself also needs to
list "what emails are waiting" and apply tags — without spinning up an agent
for those plumbing tasks. That's what this module is for.

Use ``make_source()`` to obtain the backend that matches
``CADDY_EMAIL_BACKEND``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class EmailMeta:
    """Minimal metadata the orchestrator uses to make routing decisions."""

    id: str
    subject: str
    tags: set[str] = field(default_factory=set)
    thread_id: str = ""


class EmailSource(Protocol):
    """Protocol both NotmuchSource and ImapSource implement.

    Methods are async so the IMAP backend can do network I/O without blocking.
    The notmuch backend runs subprocesses synchronously under the hood.
    """

    async def list_pending(self, limit: int = 20, days_back: int = 14) -> list[EmailMeta]:
        """Return emails that need some triage stage:

        1. NOT tag:evaluated  → need stage-1 classify
        2. tag:job_post AND NOT tag:refined  → need stage-2 refine
        3. tag:follow_up AND NOT tag:caddy_processed  → need stage-3 processor

        The orchestrator inspects ``meta.tags`` to pick which stage to run.
        ``meta.tags`` MUST be the matched message's OWN tags, not the
        thread union — a forward that shares a thread with an already-
        processed original must not inherit its ``evaluated`` markers
        (AUTO-32).
        """
        ...

    async def add_tags(self, message_id: str, tags: list[str]) -> None:
        """Idempotent tag add on a single MESSAGE by id. Safe to call with
        tags already present. Must NOT tag thread siblings — tagging at
        thread granularity poisons not-yet-processed forwards (AUTO-32)."""
        ...

    async def count_by_query(self, query: str, days_back: int = 14) -> int:
        """Thread count for an arbitrary backend query within the date
        window (powers ``caddy-inbox --status``). Backends that can't
        answer ad-hoc queries raise ``NotImplementedError``; callers
        ``hasattr``-guard before use."""
        ...

    async def list_by_query(
        self, query: str, limit: int = 20, days_back: int = 14
    ) -> list[EmailMeta]:
        """List EmailMetas matching an arbitrary backend query within the
        date window (powers ``caddy-inbox --show <state>``). Backends that
        can't answer ad-hoc queries raise ``NotImplementedError``; callers
        ``hasattr``-guard before use."""
        ...


def make_source(backend: str | None = None) -> EmailSource:
    """Resolve the active email backend.

    Precedence: explicit arg > ``CADDY_EMAIL_BACKEND`` env > ``notmuch``.
    """
    chosen = (backend or os.environ.get("CADDY_EMAIL_BACKEND", "notmuch")).lower()
    if chosen == "notmuch":
        from src.email_source.notmuch_source import NotmuchSource

        return NotmuchSource()
    if chosen == "imap":
        from src.email_source.imap_source import ImapSource

        return ImapSource()
    raise ValueError(f"Unknown CADDY_EMAIL_BACKEND {chosen!r}; expected 'notmuch' or 'imap'")
