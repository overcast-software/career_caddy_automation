"""Per-email forensic timeline for the caddy-inbox triage pipeline."""

# REMOVE WHEN STABLE — observability scaffolding for inbox debugging.
#
# `caddy-inbox` runs through five stages (classify, refine, follow-up,
# inline-post, URL-extract → JobPost). The orchestrator emits per-stage
# `logger.info` lines, but a "why didn't email X reach caddy_processed?"
# question still requires reconstructing the path from scattered log lines
# AND a logfire query. This module records every stage transition for one
# email_id as a structured event, and on `_triage_one` exit dumps a single
# JSON line with the full path so a single grep tells the whole story.
#
# Activation: `CADDY_TRIAGE_TRACE=1` (off by default — when unset, the
# context manager is a no-op aside from one cheap is_enabled() check).
#
# The `caddy-trace-email <email_id>` CLI runs a single email through
# `_triage_one` with tracing on, regardless of the env flag.

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import logfire

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.environ.get("CADDY_TRIAGE_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class _TraceEvent:
    elapsed_ms: int
    stage: str
    decision: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Tracer:
    """Per-email tracer. Methods are no-ops when CADDY_TRIAGE_TRACE is unset
    (the wrapping context manager only constructs a real Tracer when the
    flag is on; the no-op variant just discards calls)."""

    email_id: str
    subject: str
    enabled: bool
    started: float = field(default_factory=time.perf_counter)
    events: list[_TraceEvent] = field(default_factory=list)

    def event(self, stage: str, decision: str, **payload: Any) -> None:
        if not self.enabled:
            return
        elapsed = int((time.perf_counter() - self.started) * 1000)
        ev = _TraceEvent(elapsed_ms=elapsed, stage=stage, decision=decision, payload=payload)
        self.events.append(ev)
        # One logfire span per event so the triage trace shows up alongside
        # the agent spans in logfire's timeline view.
        logfire.info(
            "triage.event {stage}/{decision}",
            stage=stage,
            decision=decision,
            email_id=self.email_id,
            elapsed_ms=elapsed,
            **payload,
        )

    def dump(self, outcome: str) -> None:
        if not self.enabled:
            return
        total_ms = int((time.perf_counter() - self.started) * 1000)
        summary = {
            "email_id": self.email_id,
            "subject": self.subject,
            "outcome": outcome,
            "total_ms": total_ms,
            "events": [
                {
                    "ms": e.elapsed_ms,
                    "stage": e.stage,
                    "decision": e.decision,
                    **e.payload,
                }
                for e in self.events
            ],
        }
        logger.info("triage.trace %s", json.dumps(summary, default=str))
        logfire.info(
            "triage.trace.summary",
            email_id=self.email_id,
            subject=self.subject,
            outcome=outcome,
            total_ms=total_ms,
            event_count=len(self.events),
        )


@asynccontextmanager
async def trace_email(email_id: str, subject: str = "", *, force: bool = False):
    """Async context manager scoping a single email through the orchestrator.

    Yields a `_Tracer`. Call `t.event(stage, decision, **payload)` at every
    decision point in `_triage_one`. The final `t.dump(outcome)` runs on
    exit (whether normal or via exception); the orchestrator passes the
    outcome counter string back as the dump label.

    Pass `force=True` for the `caddy-trace-email` CLI to enable tracing
    irrespective of the env flag.
    """
    enabled = force or is_enabled()
    tracer = _Tracer(email_id=email_id, subject=subject, enabled=enabled)
    if enabled:
        with logfire.span(
            "triage.email",
            email_id=email_id,
            subject=subject,
        ):
            try:
                yield tracer
            finally:
                tracer.dump(outcome="(no outcome recorded)")
    else:
        # Fast path — no span, no events collected, no dump.
        yield tracer
