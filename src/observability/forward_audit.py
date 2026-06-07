"""Domain API for the catchall-mail forward-audit collection (Phase B3).

One doc per processed catchall message lands in ``forward_audit``.
Linked to ``triage_emails`` by ``email_id`` (RFC-822 Message-Id) when
the same email also passes through the inbox-triage pipeline — though
in practice the catchall mailbox and the operator's notmuch tree don't
overlap, so the JOIN is for future cross-flow analysis (per the B3
decision in notes.org: "new-table" over "columns on triage_emails").

All writes are wrapped: a Mongo outage logs + returns; the catchall
poller must not crash because the audit collection is down.

Outcomes — these are the buckets dashboards group by:

- ``created`` — a fresh JobPost was created (api status_code 201).
- ``deduped`` — api dedupe hit; existing JobPost returned (200).
- ``unknown_localpart`` — the resolved localpart didn't match a user;
  the poller bounced the message (or will, once SMTP submission is
  wired). ``resolved_user_id`` is None on this path.
- ``over_quota`` — the user resolved successfully but is over their
  per-day forward quota; message bounced.
- ``no_urls_extracted`` — message accepted, but the URL extractor +
  span validator returned zero job links. No POST attempted.
- ``post_failed`` — POST to the api raised or returned a non-2xx.
- ``parse_failed`` — couldn't pull a localpart from any recipient
  header; nothing actionable, message left UNSEEN for operator review.

The poller emits at most one ``forward_audit`` doc per catchall message
per pass — so daily-count aggregations on
``forwarded_to_localpart`` give the quota numbers.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


# Forward-path outcome buckets. Keep in sync with the docstring above
# and any Metabase dashboard that filters on this column.
FORWARD_OUTCOMES = frozenset(
    {
        "created",
        "deduped",
        "unknown_localpart",
        "over_quota",
        "no_urls_extracted",
        "post_failed",
        "parse_failed",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _db_or_none():
    """Acquire the cached Mongo Database; log + return None on failure.

    Mirrors the pattern in ``triage_store._db_or_none`` so the test
    suite can monkeypatch a single fake out per module.
    """
    try:
        from src.observability.mongo_client import get_db

        return get_db()
    except Exception as exc:
        logger.warning("observability: mongo unreachable (%s); forward_audit skipped", exc)
        return None


def record_forward_audit(
    *,
    email_id: str,
    forwarded_to_localpart: str | None,
    forwarded_via_address: str | None,
    resolved_user_id: int | None,
    outcome: str,
    job_post_id: int | str | None = None,
    quota_remaining: int | None = None,
    bounce_reason: str | None = None,
    subject: str | None = None,
    sender: str | None = None,
    extras: dict[str, Any] | None = None,
) -> None:
    """Insert one ``forward_audit`` doc.

    ``email_id`` is the RFC-822 Message-Id of the forwarded email — the
    catchall-mailbox copy, not whatever inbound provenance the user's
    own forward inherited. It's the stable JOIN key against
    ``triage_emails`` if a future flow needs to correlate catchall +
    inbox processing of the same thread.

    ``outcome`` should be one of :data:`FORWARD_OUTCOMES`; an unknown
    value is logged and stored anyway (don't crash the poller because
    we want to evolve outcomes without coordinating a deploy).
    """
    if outcome not in FORWARD_OUTCOMES:
        logger.warning("forward_audit: unknown outcome %r — storing anyway", outcome)
    db = _db_or_none()
    if db is None:
        return
    try:
        doc: dict[str, Any] = {
            "email_id": email_id,
            "forwarded_to_localpart": forwarded_to_localpart,
            "forwarded_via_address": forwarded_via_address,
            "resolved_user_id": resolved_user_id,
            "outcome": outcome,
            "job_post_id": str(job_post_id) if job_post_id is not None else None,
            "quota_remaining": quota_remaining,
            "bounce_reason": bounce_reason,
            "subject": subject,
            "sender": sender,
            "recorded_at": _now(),
        }
        if extras:
            doc.update(extras)
        db.forward_audit.insert_one(doc)
    except Exception as exc:
        logger.warning("observability: record_forward_audit failed for %s: %s", email_id, exc)


def count_forwards_today(user_id: int) -> int:
    """Return how many ``forward_audit`` docs the user has from the
    last 24 hours, regardless of outcome. The quota check at the
    poller is "how many *attempts* in the rolling 24h window" — not
    just successful creates — so a user spamming "no_urls_extracted"
    mails still hits quota.

    Returns ``0`` on Mongo outage; the poller fails-open by default
    (better to over-process during an observability gap than to
    silently drop user mail).
    """
    db = _db_or_none()
    if db is None:
        return 0
    try:
        since = _now() - timedelta(hours=24)
        return int(
            db.forward_audit.count_documents(
                {
                    "resolved_user_id": user_id,
                    "recorded_at": {"$gte": since},
                }
            )
        )
    except Exception as exc:
        logger.warning("observability: count_forwards_today failed for user %s: %s", user_id, exc)
        return 0
