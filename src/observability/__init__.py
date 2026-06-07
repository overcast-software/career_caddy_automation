"""Mongo-backed observability for cc_auto pipelines.

Domain APIs (call these from pipeline code; never reach into pymongo
directly):

- ``start_run(backend)`` / ``record_email(...)`` / ``finish_run(...)``
  — triage_runs + triage_emails collections.
- ``record_skipped_duplicate(...)`` — dedupe-skip log.

Connection plumbing in ``mongo_client.py``. Writes are
fire-and-forget operator-side; we never let an observability failure
crash the hot path (every domain API catches ``PyMongoError``).
"""

from src.observability.forward_audit import (
    FORWARD_OUTCOMES,
    count_forwards_today,
    record_forward_audit,
)
from src.observability.triage_store import (
    classify_exception,
    finish_run,
    record_email,
    record_skipped_duplicate,
    start_run,
)

__all__ = [
    "FORWARD_OUTCOMES",
    "classify_exception",
    "count_forwards_today",
    "finish_run",
    "record_email",
    "record_forward_audit",
    "record_skipped_duplicate",
    "start_run",
]
