"""Notmuch implementation of :class:`EmailSource`.

Shells out to the ``notmuch`` CLI the same way
``scripts/tag_emails.py`` and ``scripts/process_tagged.py`` do. Nothing
fancy — just the subset the orchestrator needs.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta
from email.header import decode_header

from src.email_source import EmailMeta

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
            query_arr = thread.get("query") or []
            if not query_arr or not query_arr[0]:
                continue
            raw_id = query_arr[0]
            if raw_id.startswith("id:"):
                raw_id = raw_id[3:]
            out.append(
                EmailMeta(
                    id=raw_id,
                    subject=_decode_subject(thread.get("subject") or ""),
                    tags=set(thread.get("tags") or []),
                )
            )
        return out

    async def add_tags(self, email_id: str, tags: list[str]) -> None:
        if not tags:
            return
        args = [f"+{t}" for t in tags]
        subprocess.run(
            ["notmuch", "tag", *args, "--", f'id:"{email_id}"'],
            check=True,
            timeout=10,
        )
