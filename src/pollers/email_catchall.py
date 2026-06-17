"""Catchall-mailbox → JobPost poller (Phase B3).

Reads UNSEEN messages from the catchall IMAP mailbox, resolves the
``<localpart>@careercaddy.online`` recipient → Career Caddy user via
the staff ``GET /api/v1/users/?filter[username]=…`` endpoint, runs the
existing URL extractor + span validator over the body, and POSTs the
resulting JobPosts with ``source="email-forward"`` provenance plus the
catchall-specific attributes ``forwarded_via_address`` +
``discover_for_user_id`` (api PRs #149/#150/#151, 2026-06).

Per the standing cc_auto rule (``feedback_inbox_no_auto_scrape``): the
poller creates JobPost rows ONLY and the user initiates scrapes from
the UI on posts they want pulled.

The ONE authorized exception is opt-in and narrow: when
``CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD`` is enabled AND a freshly-created
JobPost's link points at a *known-good* scrape domain (per the api's
per-domain readiness signal, ScrapeProfile filter endpoint, api PR
#185), the poller creates a single ``hold`` Scrape for that post so the
scrape runner pulls it. The flag is OFF by default, so production
behavior is unchanged until an operator explicitly enables it. Every
failure mode (flag off, host not known-good, profile fetch error,
dedupe/quota-skip) fails safe to JobPost-only — auto-scrape never
blocks or fails JobPost discovery, and the decision is per-URL and
independent.

Per-user-per-day quota check + per-message ``forward_audit`` writes
land in Mongo. The poller fails open on Mongo outage — every call
into the observability layer is wrapped.

Bounce path (unknown localpart / over quota): logged + audited but
*not* relayed in this initial cut. Wiring SMTP submission needs the
operator-side MTA config (per notes.org/Phase B2). Until then, the
unprocessed message is left UNSEEN so the operator can review it,
and a ``forward_audit`` doc records why the poller skipped it.

Configuration (all env-driven; no positional secrets):

- ``CC_API_BASE_URL``, ``CC_API_TOKEN`` — Career Caddy api endpoint +
  staff API key (Bearer scheme; see notes.org Operations/Bearer trap).
- ``CADDY_CATCHALL_IMAP_HOST/PORT/USER/PASS/MAILBOX`` — IMAP
  credentials. See ``src.email_source.imap_source.CatchallImapClient``.
- ``CADDY_CATCHALL_DOMAIN`` — catchall domain (default
  ``careercaddy.online``).
- ``CADDY_FORWARD_QUOTA_PER_USER_PER_DAY`` — soft quota (default 100).

Run forms:

    uv run caddy-catchall                       # one-shot
    uv run caddy-catchall --loop                # loop forever
    uv run caddy-catchall --loop --interval 60  # explicit poll interval
    uv run caddy-catchall --once --limit 10
"""

from lib.observability import configure_logfire

configure_logfire("caddy-catchall")

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from src.agents.span_validator import filter_span_atomic
from src.agents.url_extractor import extract_job_urls
from src.client.api_client import (
    ApiClient,
    create_job_post_minimal,
    create_job_post_with_company_check,
    create_scrape,
    fetch_profile_readiness,
    find_user_by_username,
)
from src.email_source.imap_source import CatchallImapClient, CatchallMessage
from src.observability import (
    count_forwards_today,
    record_forward_audit,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


DEFAULT_QUOTA = 100
DEFAULT_INTERVAL_S = 60
DEFAULT_LIMIT = 20


# ---------------------------------------------------------------------------
# Pure helpers (tests drive these directly).
# ---------------------------------------------------------------------------


@dataclass
class ProcessOutcome:
    """What :func:`process_one` reports back for the audit row + log line."""

    outcome: str  # one of observability.FORWARD_OUTCOMES
    job_post_id: int | str | None = None
    quota_remaining: int | None = None
    bounce_reason: str | None = None
    created: int = 0
    deduped: int = 0
    failed: int = 0
    # Known-good auto-scrape decision (opt-in; see _maybe_auto_scrape).
    scrape_created: bool = False
    scrape_id: int | None = None
    profile_tier: str | None = None


async def resolve_localpart(api: ApiClient, localpart: str) -> int | None:
    """Resolve ``<localpart>@careercaddy.online`` → Career Caddy user id.

    Returns the matched user's id on success, or ``None`` for unknown.

    Trusts the api's filter validation (api PR #151's catchall validator)
    — a syntactically invalid username returns an empty list rather than
    raising. Network failures propagate (the caller logs + audits).
    """
    raw = await find_user_by_username(api, localpart)
    try:
        resp = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not resp.get("success"):
        return None
    users = (resp.get("data") or {}).get("data") or []
    if not users:
        return None
    try:
        return int(users[0]["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _interpret_post_response(raw: str, link: str | None) -> tuple[str, int | str | None]:
    """Map an api response to ``(outcome, post_id)`` per the
    dedupe-first walk:

    - status 201 (or any other 2xx fresh-create) → ``"created"``
    - status 200 (dedupe / merge-onto-existing)  → ``"deduped"``
    - status 4xx / non-success                   → ``"post_failed"``

    Mirrors the response interpretation in
    ``scripts.inbox_triage._create_posts_from_urls`` so the catchall
    poller and the inbox-triage URL stage stay in lockstep on what
    "duplicate" means.
    """
    try:
        resp = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("catchall: unparseable api response for %s: %s", link, exc)
        return "post_failed", None
    if not resp.get("success"):
        logger.warning("catchall: api error for %s: %s", link, resp.get("error"))
        return "post_failed", None
    post_resource = (resp.get("data") or {}).get("data") or {}
    post_id = post_resource.get("id")
    status_code = resp.get("status_code")
    if status_code == 200:
        return "deduped", post_id
    return "created", post_id


def _forward_auto_scrape_enabled() -> bool:
    """Opt-in gate for the known-good auto-scrape exception.

    Default OFF. Mirrors the truthy-string contract of
    ``scripts.process_tagged._auto_scrape_enabled`` so operators have one
    mental model for both auto-scrape flags. When OFF (the production
    default), the catchall poller stays JobPost-only — it never calls
    ``create_scrape``.
    """
    return os.environ.get("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _maybe_auto_scrape(
    api: ApiClient,
    *,
    url: str,
    job_post_id: int | str | None,
) -> tuple[int | None, str | None]:
    """Known-good auto-scrape decision for one freshly-created JobPost.

    The DELIBERATE, user-authorized exception to
    ``feedback_inbox_no_auto_scrape``: only when
    ``CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD`` is enabled AND ``url``'s
    hostname is a known-good scrape domain do we create a ``hold`` Scrape
    for the post so the scrape runner pulls it.

    Returns ``(scrape_id, profile_tier)`` — ``scrape_id`` is non-None only
    when a scrape was actually created; ``profile_tier`` carries the
    readiness tier of the profile that gated the decision (or ``None``).

    Fully fail-safe: flag off, missing post id, unparseable host, unknown
    host, not-known-good, profile-fetch error, or scrape-create failure
    all return without creating a scrape and never raise. Auto-scrape
    must NEVER block or fail JobPost discovery.
    """
    if not _forward_auto_scrape_enabled():
        return None, None
    if job_post_id is None:
        return None, None
    try:
        jp_id = int(job_post_id)
    except (TypeError, ValueError):
        return None, None
    hostname = urlparse(url).hostname
    if not hostname:
        return None, None

    try:
        readiness = await fetch_profile_readiness(api, hostname)
    except Exception as exc:  # fetch_profile_readiness is fail-safe, but belt-and-suspenders.
        logger.warning("catchall: profile readiness fetch raised for %s: %s", hostname, exc)
        return None, None
    if readiness is None:
        return None, None
    is_known_good, tier = readiness
    if not is_known_good:
        return None, tier

    try:
        raw = await create_scrape(api, url=url, job_post_id=jp_id, status="hold")
        resp = json.loads(raw)
    except Exception as exc:
        logger.warning("catchall: auto-scrape create raised for post %s: %s", jp_id, exc)
        return None, tier
    if not resp.get("success"):
        logger.warning(
            "catchall: auto-scrape create failed for post %s: %s", jp_id, resp.get("error")
        )
        return None, tier

    scrape_resource = (resp.get("data") or {}).get("data") or {}
    raw_id = scrape_resource.get("id")
    try:
        scrape_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        scrape_id = None
    logger.info("[FWD-SCRAPE] post=%s host=%s tier=%s scrape=%s", jp_id, hostname, tier, scrape_id)
    return scrape_id, tier


async def process_one(
    api: ApiClient,
    msg: CatchallMessage,
    *,
    quota: int = DEFAULT_QUOTA,
    pipeline_run_id: str | None = None,
) -> ProcessOutcome:
    """Process one catchall message end-to-end.

    Returns a :class:`ProcessOutcome` for the audit row. Does NOT write
    the audit doc itself — the caller does, so a unit test can inspect
    both the outcome and what would have been audited without needing
    Mongo.
    """
    if not msg.forwarded_to_localpart:
        return ProcessOutcome(
            outcome="parse_failed",
            bounce_reason="no catchall-domain recipient on the message",
        )

    localpart = msg.forwarded_to_localpart
    user_id = await resolve_localpart(api, localpart)
    if user_id is None:
        return ProcessOutcome(
            outcome="unknown_localpart",
            bounce_reason=f"no user with username={localpart!r}",
        )

    today_count = count_forwards_today(user_id)
    quota_remaining = max(0, quota - today_count)
    if today_count >= quota:
        return ProcessOutcome(
            outcome="over_quota",
            quota_remaining=0,
            bounce_reason=f"user {user_id} hit {quota}/day forward quota",
        )

    # URL extraction + cross-row hallucination guard.
    api_token = os.environ.get("CC_API_TOKEN", "")
    extracted = await extract_job_urls(
        msg.body_text,
        api_token=api_token,
        pipeline_run_id=pipeline_run_id,
    )
    safe_links = filter_span_atomic(
        extracted.job_urls, msg.body_text, email_id=msg.message_id or msg.uid
    )
    if not safe_links:
        return ProcessOutcome(
            outcome="no_urls_extracted",
            quota_remaining=quota_remaining,
            bounce_reason=(
                f"url_extractor returned 0 actionable links "
                f"(reasoning: {extracted.reasoning[:120]})"
            ),
        )

    # POST one JobPost per extracted link. The api owns dedupe; we
    # tally created vs. deduped from the per-row response codes.
    created = 0
    deduped = 0
    failed = 0
    first_post_id: int | str | None = None
    scrape_created = False
    first_scrape_id: int | None = None
    first_scrape_tier: str | None = None
    for link in safe_links:
        desc = link.description or None
        try:
            if link.company:
                raw = await create_job_post_with_company_check(
                    api,
                    title=link.title,
                    company_name=link.company,
                    link=link.url,
                    description=desc,
                    source="email-forward",
                    forwarded_via_address=msg.forwarded_via_address,
                    discover_for_user_id=user_id,
                )
            else:
                raw = await create_job_post_minimal(
                    api,
                    title=link.title,
                    link=link.url,
                    description=desc,
                    source="email-forward",
                    forwarded_via_address=msg.forwarded_via_address,
                    discover_for_user_id=user_id,
                )
        except Exception as exc:
            logger.warning("catchall: POST raised for %s: %s", link.url, exc)
            failed += 1
            continue
        outcome, post_id = _interpret_post_response(raw, link.url)
        if outcome == "created":
            created += 1
            if first_post_id is None:
                first_post_id = post_id
            logger.info(
                "[FWD] uid=%s user=%s  %s @ %s  id=%s",
                msg.uid,
                user_id,
                link.title[:40],
                link.company or "—",
                post_id,
            )
            # Opt-in known-good auto-scrape — created posts only, never
            # dedupes. Per-URL and fail-safe; defaults to JobPost-only.
            s_id, s_tier = await _maybe_auto_scrape(api, url=link.url, job_post_id=post_id)
            if s_id is not None:
                scrape_created = True
                if first_scrape_id is None:
                    first_scrape_id = s_id
                    first_scrape_tier = s_tier
        elif outcome == "deduped":
            deduped += 1
            if first_post_id is None:
                first_post_id = post_id
            logger.info(
                "[FWD-DUP] uid=%s user=%s  %s  id=%s",
                msg.uid,
                user_id,
                link.title[:40],
                post_id,
            )
        else:
            failed += 1

    if failed and not (created or deduped):
        return ProcessOutcome(
            outcome="post_failed",
            quota_remaining=max(0, quota_remaining - created),
            bounce_reason="all JobPost POSTs failed",
            failed=failed,
        )

    primary_outcome = "created" if created else "deduped"
    return ProcessOutcome(
        outcome=primary_outcome,
        job_post_id=first_post_id,
        quota_remaining=max(0, quota_remaining - created),
        created=created,
        deduped=deduped,
        failed=failed,
        scrape_created=scrape_created,
        scrape_id=first_scrape_id,
        profile_tier=first_scrape_tier,
    )


# ---------------------------------------------------------------------------
# Orchestrator — connects to IMAP, fetches messages, drives process_one.
# ---------------------------------------------------------------------------


def _audit_msg(msg: CatchallMessage, user_id: int | None, outcome: ProcessOutcome) -> None:
    """Write one ``forward_audit`` doc per processed message."""
    record_forward_audit(
        email_id=msg.message_id or msg.uid,
        forwarded_to_localpart=msg.forwarded_to_localpart,
        forwarded_via_address=msg.forwarded_via_address,
        resolved_user_id=user_id,
        outcome=outcome.outcome,
        job_post_id=outcome.job_post_id,
        quota_remaining=outcome.quota_remaining,
        bounce_reason=outcome.bounce_reason,
        subject=msg.subject,
        sender=msg.sender,
        scrape_created=outcome.scrape_created,
        scrape_id=outcome.scrape_id,
        profile_tier=outcome.profile_tier,
        extras={
            "uid": msg.uid,
            "mailbox": msg.mailbox,
            "raw_size": msg.raw_size,
            "to_addresses": msg.to_addresses,
            "counts": {
                "created": outcome.created,
                "deduped": outcome.deduped,
                "failed": outcome.failed,
            },
        },
    )


def _api_client() -> ApiClient:
    return ApiClient(
        os.environ.get("CC_API_BASE_URL", "http://localhost:8000"),
        os.environ["CC_API_TOKEN"],
    )


def _quota_from_env() -> int:
    try:
        return int(os.environ.get("CADDY_FORWARD_QUOTA_PER_USER_PER_DAY", DEFAULT_QUOTA))
    except ValueError:
        logger.warning(
            "CADDY_FORWARD_QUOTA_PER_USER_PER_DAY not an int; falling back to %d", DEFAULT_QUOTA
        )
        return DEFAULT_QUOTA


# Outcomes that mean the poller acknowledged the message; the IMAP UID
# can safely be marked \Seen. Anything else leaves the message
# UNSEEN so the next pass picks it up (transient network, parser bug,
# or operator review on unknown_localpart / over_quota until the bounce
# relay is wired in B2).
_ACKABLE_OUTCOMES = frozenset({"created", "deduped", "no_urls_extracted"})


async def run_once(limit: int = DEFAULT_LIMIT) -> dict[str, int]:
    """One sweep of the catchall mailbox. Returns the outcome counters."""
    quota = _quota_from_env()
    counters: dict[str, int] = {}

    async with CatchallImapClient() as imap:
        messages = await imap.fetch_unseen(limit=limit)
        if not messages:
            logger.info("catchall: no unseen messages")
            return counters

        api = _api_client()
        for msg in messages:
            user_id: int | None = None
            try:
                # Look up user_id first so the audit row carries it
                # even when downstream POST raises.
                if msg.forwarded_to_localpart:
                    user_id = await resolve_localpart(api, msg.forwarded_to_localpart)
                outcome = await process_one(api, msg, quota=quota)
            except Exception as exc:
                logger.exception("catchall: process_one raised for uid=%s: %s", msg.uid, exc)
                outcome = ProcessOutcome(
                    outcome="post_failed", bounce_reason=f"{type(exc).__name__}: {exc}"
                )
            try:
                _audit_msg(msg, user_id, outcome)
            except Exception as exc:
                logger.warning("catchall: audit write failed for uid=%s: %s", msg.uid, exc)

            counters[outcome.outcome] = counters.get(outcome.outcome, 0) + 1
            if outcome.outcome in _ACKABLE_OUTCOMES:
                try:
                    await imap.mark_processed(msg.uid)
                except Exception as exc:
                    logger.warning("catchall: mark_processed failed for uid=%s: %s", msg.uid, exc)

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
    logger.info("catchall done: %s", summary)
    return counters


async def main() -> None:
    parser = argparse.ArgumentParser(description="Catchall mailbox → JobPost poller (B3).")
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument("--once", action="store_true", help="Single sweep then exit.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max messages per sweep.")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_S,
        help="Seconds between sweeps when --loop.",
    )
    args = parser.parse_args()

    if args.once or not args.loop:
        await run_once(limit=args.limit)
        return

    while True:
        try:
            await run_once(limit=args.limit)
        except Exception as exc:
            logger.exception("catchall: run_once raised: %s", exc)
        await asyncio.sleep(args.interval)


def run() -> None:
    """``[project.scripts]`` entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
