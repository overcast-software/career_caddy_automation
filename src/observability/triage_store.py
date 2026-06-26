"""Domain API for the triage observability collections.

Three documents per run end up in Mongo:

- ``triage_runs`` — one doc per ``run_once()``. ``_id`` is reused as
  the run_id foreign key in the per-email docs.
- ``triage_emails`` — one doc per email processed (every email gets a
  row, even fall-throughs to ``already_done``, so the count matches
  the loop count).
- ``skipped_duplicates`` — one doc per JobPost the dedup pre-pass
  declined to POST.

All writes are wrapped — a Mongo outage or schema mismatch logs a
warning but never raises into the caller. Observability is the
servant, not the master, of the triage loop.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# Outcome buckets for the ``error`` path; used by classify_exception().
_NETWORK_EXCEPTION_NAMES = {
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "NetworkError",
    "APIConnectionError",
    "APITimeoutError",
}
_LLM_EXCEPTION_NAMES = {
    "APIError",
    "APIStatusError",
    "RateLimitError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "UnprocessableEntityError",
    "InternalServerError",
    "ServiceUnavailableError",
}


def classify_exception(exc: BaseException) -> tuple[str, bool]:
    """Map an exception to a refined outcome bucket + ``network_failure`` flag.

    Returns ``(bucket, network_failure)`` where ``bucket`` is one of
    ``network_error``, ``llm_error``, ``unknown_error``. Per the Phase A1
    "both bucket and exception_class" decision in notes.org — bucket is
    one-shot queryable for Metabase histograms; the caller stores the
    full class name separately.
    """
    name = type(exc).__name__
    if name in _NETWORK_EXCEPTION_NAMES:
        return "network_error", True
    if name in _LLM_EXCEPTION_NAMES:
        return "llm_error", False
    # httpx exceptions fall under ``httpx.*Error`` family — class-name
    # heuristic catches them without importing httpx here.
    if name.endswith("TimeoutError") or name.endswith("ConnectError"):
        return "network_error", True
    return "unknown_error", False


def _now() -> datetime:
    return datetime.now(UTC)


def _db_or_none():
    """Try to acquire the database handle; log + return None on failure.

    Hot-path callers should never see an exception from observability.
    """
    try:
        from src.observability.mongo_client import get_db

        return get_db()
    except Exception as exc:
        logger.warning("observability: mongo unreachable (%s); writes skipped", exc)
        return None


def start_run(backend: str | None) -> Any | None:
    """Insert a new ``triage_runs`` doc and return its ``_id``.

    Return value is opaque (typically ``bson.ObjectId``) — callers pass
    it back to ``record_email`` / ``finish_run`` without inspecting it.
    Returns ``None`` if the write failed; downstream helpers tolerate a
    ``None`` run_id and just skip their writes.
    """
    db = _db_or_none()
    if db is None:
        return None
    try:
        doc = {
            "started_at": _now(),
            "finished_at": None,
            "backend": backend or "notmuch",
            "total_emails": None,
            "counters": None,
        }
        result = db.triage_runs.insert_one(doc)
        return result.inserted_id
    except Exception as exc:
        logger.warning("observability: start_run failed: %s", exc)
        return None


def record_email(
    run_id: Any | None,
    email_id: str,
    subject: str | None,
    outcome: str,
    tags_added: list[str],
    *,
    exception_class: str | None = None,
    network_failure: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """Insert one ``triage_emails`` doc for a processed email.

    ``run_id`` may be ``None`` if ``start_run`` failed — we still write
    the row (with run_id=None) so partial-outage data is preserved.
    """
    if run_id is None:
        # If we don't even have a run anchor, the write is informational
        # only; preserve it anyway so a Mongo-up / earlier-failed sequence
        # doesn't silently drop emails.
        pass
    db = _db_or_none()
    if db is None:
        return
    try:
        doc: dict[str, Any] = {
            "run_id": run_id,
            "email_id": email_id,
            "subject": subject,
            "outcome": outcome,
            "tags_added": list(tags_added),
            "exception_class": exception_class,
            "network_failure": network_failure,
            "processed_at": _now(),
        }
        if extra:
            doc.update(extra)
        db.triage_emails.insert_one(doc)
    except Exception as exc:
        logger.warning("observability: record_email failed for %s: %s", email_id, exc)


def record_skipped_duplicate(
    run_id: Any | None,
    email_id: str,
    *,
    incoming_title: str | None,
    incoming_company: str | None,
    incoming_link: str | None,
    matched_post_id: str | None,
    confidence: float | None,
    reason: str | None,
    source: str | None,
) -> None:
    """Insert one ``skipped_duplicates`` doc for a deduped JobPost.

    Distinct from the per-email outcome string — keeps the matched
    post id / confidence / reason so a false-positive audit is one
    query.
    """
    db = _db_or_none()
    if db is None:
        return
    try:
        doc = {
            "run_id": run_id,
            "email_id": email_id,
            "incoming_title": incoming_title,
            "incoming_company": incoming_company,
            "incoming_link": incoming_link,
            "matched_post_id": matched_post_id,
            "confidence": confidence,
            "reason": reason,
            "source": source,
            "recorded_at": _now(),
        }
        db.skipped_duplicates.insert_one(doc)
    except Exception as exc:
        logger.warning("observability: record_skipped_duplicate failed: %s", exc)


def finish_run(
    run_id: Any | None,
    total_emails: int,
    counters: dict[str, int],
) -> None:
    """Patch the ``triage_runs`` doc with the finish time + summary counters."""
    if run_id is None:
        return
    db = _db_or_none()
    if db is None:
        return
    try:
        db.triage_runs.update_one(
            {"_id": run_id},
            {
                "$set": {
                    "finished_at": _now(),
                    "total_emails": total_emails,
                    "counters": counters,
                }
            },
        )
    except Exception as exc:
        logger.warning("observability: finish_run failed: %s", exc)
