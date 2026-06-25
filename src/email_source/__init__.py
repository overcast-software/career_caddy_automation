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

_DEFAULT_INBOX_NOTMUCH_FOLDER = "forwarding@careercaddy.online"


def notmuch_folder_scope() -> str | None:
    """Resolve the notmuch maildir folder the inbox pipeline is scoped to.

    Reads ``CADDY_INBOX_NOTMUCH_FOLDER`` with three-way precedence:

    * **unset** → the default multi-user catchall folder
      (``forwarding@careercaddy.online``). The server-side catchall
      (``*@careercaddy.online``) collects every user's forwarded job mail
      into this one folder; scoping to it by default keeps the triage
      pipeline off unrelated mail in the index.
    * **set & non-empty** → that folder verbatim (operator override).
    * **set & empty** (incl. whitespace-only) → ``None``: legacy
      whole-index behaviour. This is the OSS / un-pre-filtered escape
      hatch — operators who route job mail into their own folder, or who
      run a dedicated job-only notmuch DB, opt out of the catchall scope
      and the source queries the entire index as it did before AUTO-20.
    """
    raw = os.environ.get("CADDY_INBOX_NOTMUCH_FOLDER")
    if raw is None:
        return _DEFAULT_INBOX_NOTMUCH_FOLDER
    raw = raw.strip()
    return raw or None


@dataclass
class EmailMeta:
    """Minimal metadata the orchestrator uses to make routing decisions."""

    id: str
    subject: str
    tags: set[str] = field(default_factory=set)
    thread_id: str = ""
    # Envelope recipient (the catchall RCPT, e.g. ``<user>@careercaddy.online``).
    # Lazily populated — see ``EmailSource.get_recipient``; ``None`` until then.
    recipient: str | None = None


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
        """
        ...

    async def add_tags(self, thread_id: str, tags: list[str]) -> None:
        """Idempotent tag add. Safe to call with tags already present."""
        ...

    async def get_recipient(self, email_id: str) -> str | None:
        """Return the envelope recipient address for a single message.

        For the multi-user catchall this is the ``<username>@careercaddy.online``
        address the message was *delivered* to — the authoritative RCPT used
        downstream (AUTO-24) to attribute each forwarded JobPost to the right
        user. Prefers the envelope recipient over the original ``To:``; returns
        the raw address (no username resolution) or ``None`` when none is found.
        """
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
