"""caddy-trace-email <email_id> — run one email through caddy-inbox with full tracing."""

# REMOVE WHEN STABLE — observability scaffolding for inbox debugging.
#
# The full caddy-inbox loop with `--once --limit 1` and a notmuch query for a
# specific email_id, with CADDY_TRIAGE_TRACE forced on for this single run.
# Useful to reproduce a regression scrape-style: pinpoint a problem email,
# run trace, get a single JSON timeline line back.

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from src.email_source import EmailMeta
from src.email_source.notmuch_source import NotmuchSource


async def trace_one(email_id: str) -> int:
    os.environ["CADDY_TRIAGE_TRACE"] = "1"
    os.environ.setdefault("CADDY_DEDUPE_TRACE", "1")

    # Late imports so the env flags above take effect first.
    from lib.trace_inbox import trace_email
    from scripts.inbox_triage import (
        _api_client,
        _caddy_deps,
        _triage_one,
    )
    from src.agents.email_agents import (
        get_classify_agent,
        get_followup_agent,
        get_inline_post_agent,
        get_refine_agent,
    )

    source = NotmuchSource()

    # Build an EmailMeta the orchestrator will accept. We don't have the
    # email's tag set or subject from this end — fetch them via notmuch.
    pending = await source.list_pending(limit=200, days_back=365)
    meta: EmailMeta | None = next((m for m in pending if m.id == email_id), None)
    if meta is None:
        # Fall back: build a synthetic meta and let the orchestrator
        # re-derive tags. Stage 1 will re-classify if `evaluated` is missing.
        meta = EmailMeta(id=email_id, subject="(unknown — not in pending list)", tags=set())

    api = _api_client()
    deps = _caddy_deps()

    async with trace_email(meta.id, meta.subject, force=True) as t:
        outcome = await _triage_one(
            meta,
            source,
            get_classify_agent(),
            get_refine_agent(),
            get_followup_agent(),
            get_inline_post_agent(),
            api,
            deps,
        )
        t.event("triage", "done", outcome=outcome)
        t.dump(outcome)
    print(f"\noutcome: {outcome}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run a single email through caddy-inbox with full tracing on. "
            "Reads CC_API_TOKEN + CC_API_BASE_URL from env. Emits a "
            "triage.trace JSON line and per-stage logfire spans."
        ),
    )
    parser.add_argument("email_id", type=str, help="The notmuch email id to trace.")
    args = parser.parse_args()
    sys.exit(asyncio.run(trace_one(args.email_id)))


if __name__ == "__main__":
    main()
