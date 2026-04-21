"""Orchestrator — classify, refine, and (where applicable) process follow-ups
on a single sequential pass per email.

Runs three stateless agents in order so they never race on the same
notmuch/IMAP tags:

    stage 1 (classify)  →  tag `evaluated`; if job-related, tag `job_post`
    stage 2 (refine)    →  tag `refined`; if correspondence, tag `follow_up`
    stage 3 (followup)  →  find the matching job_application; on confident
                           match, update its status and tag `caddy_processed`

Backend is chosen by ``CADDY_EMAIL_BACKEND`` (``notmuch`` default, ``imap``
when implemented).

**Do not run this alongside** ``caddy-classify`` / ``caddy-process`` against
the same mailbox — they mutate the same tags and will race.

Usage:
    uv run caddy-inbox                       # loop every 15 minutes
    uv run caddy-inbox --once --limit 5
    uv run caddy-inbox --backend notmuch
"""

from lib.observability import configure_logfire

configure_logfire("caddy-inbox")

import argparse
import asyncio
import json
import logging
import os

from src.agents.email_agents import (
    FollowupResult,
    get_classify_agent,
    get_followup_agent,
    get_refine_agent,
)
from src.client.api_client import ApiClient, get_job_applications, update_job_application
from src.client.toolset import CareerCaddyDeps
from src.email_source import EmailMeta, EmailSource, make_source

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


CONFIDENCE_FLOOR = 0.6


def _caddy_deps() -> CareerCaddyDeps:
    return CareerCaddyDeps(
        api_token=os.environ["CC_API_TOKEN"],
        base_url=os.environ.get("CC_API_BASE_URL", "http://localhost:8000"),
    )


def _api_client() -> ApiClient:
    return ApiClient(
        os.environ.get("CC_API_BASE_URL", "http://localhost:8000"),
        os.environ["CC_API_TOKEN"],
    )


async def _current_app_status(api: ApiClient, application_id: int) -> str | None:
    """Fetch the current status of a job_application so we can skip no-op
    PATCHes. Returns None if the fetch fails — callers treat None as 'unknown'
    and proceed with the update."""
    try:
        raw = await get_job_applications(api, id=application_id)
        resp = json.loads(raw)
        data = (resp.get("data") or {}).get("data") or resp.get("data")
        if isinstance(data, dict):
            attrs = data.get("attributes") or data
            return attrs.get("status") if isinstance(attrs, dict) else None
    except Exception as exc:
        logger.warning("  could not fetch current status for app %s: %s", application_id, exc)
    return None


async def _run_classify(agent, email_id: str) -> bool:
    """Return True iff the email is job-related."""
    result = await agent.run(f"Classify email id: {email_id}")
    text = (result.output or "").strip().lower()
    return text.startswith("job_post")


async def _run_refine(agent, email_id: str):
    result = await agent.run(f"Refine email id: {email_id}")
    return result.output


async def _run_followup(agent, email_id: str, deps: CareerCaddyDeps) -> FollowupResult:
    result = await agent.run(f"Process follow-up email id: {email_id}", deps=deps)
    return result.output


async def _apply_status_update(api: ApiClient, res: FollowupResult) -> bool:
    """PATCH the application if the new status differs. Returns True on
    success (including no-op)."""
    assert res.application_id is not None
    current = await _current_app_status(api, res.application_id)
    if current and current == res.new_status:
        logger.info("  app %s already %s — no update", res.application_id, current)
        return True
    try:
        raw = await update_job_application(
            api,
            application_id=res.application_id,
            status=res.new_status,
            notes=res.notes,
        )
        resp = json.loads(raw)
        if not resp.get("success"):
            logger.warning("  update_job_application failed: %s", resp.get("error"))
            return False
        logger.info(
            "  app %s: %s → %s  (%s)",
            res.application_id,
            current or "?",
            res.new_status,
            res.evidence[:80],
        )
        return True
    except Exception as exc:
        logger.exception("  update_job_application raised: %s", exc)
        return False


async def _triage_one(
    meta: EmailMeta,
    source: EmailSource,
    classify_agent,
    refine_agent,
    followup_agent,
    api: ApiClient,
    deps: CareerCaddyDeps,
) -> str:
    """Drive a single email through whichever stages it still needs. Returns
    a short status string for the summary counter."""
    email_id = meta.id
    tags = set(meta.tags)

    # Stage 1 — classify (only if not yet evaluated).
    if "evaluated" not in tags:
        is_job = await _run_classify(classify_agent, email_id)
        new_tags = ["evaluated"] + (["job_post"] if is_job else [])
        await source.add_tags(email_id, new_tags)
        tags.update(new_tags)
        logger.info("[%s] %s  %s", "JOB" if is_job else "---", email_id, meta.subject)
        if not is_job:
            return "not_job"

    # Stage 2 — refine (only job-related, only if not yet refined).
    if "job_post" in tags and "refined" not in tags:
        refined = await _run_refine(refine_agent, email_id)
        new_tags = ["refined"]
        is_followup = refined.kind == "follow_up" and refined.confidence >= CONFIDENCE_FLOOR
        if is_followup:
            new_tags.append("follow_up")
        await source.add_tags(email_id, new_tags)
        tags.update(new_tags)
        logger.info(
            "[%s] %s  conf=%.2f  %s",
            "FUP" if is_followup else "NEW",
            email_id,
            refined.confidence,
            refined.evidence[:80],
        )
        if not is_followup:
            return "new_post"

    # Stage 3 — follow-up processor (only if follow_up and not yet processed).
    if "follow_up" in tags and "caddy_processed" not in tags:
        res = await _run_followup(followup_agent, email_id, deps)
        if (
            res.application_id is not None
            and res.new_status is not None
            and res.confidence >= CONFIDENCE_FLOOR
        ):
            ok = await _apply_status_update(api, res)
            if ok:
                await source.add_tags(email_id, ["caddy_processed"])
                return "processed"
            return "update_failed"
        logger.info(
            "  no confident application match for %s (conf=%.2f): %s",
            email_id,
            res.confidence,
            res.notes[:120],
        )
        return "unmatched"

    return "already_done"


async def run_once(limit: int, backend: str | None, days_back: int) -> None:
    source = make_source(backend)
    pending = await source.list_pending(limit=limit, days_back=days_back)
    if not pending:
        logger.info("No pending emails.")
        return

    classify_agent = get_classify_agent()
    refine_agent = get_refine_agent()
    followup_agent = get_followup_agent()
    api = _api_client()
    deps = _caddy_deps()

    counters: dict[str, int] = {}
    for meta in pending:
        try:
            outcome = await _triage_one(
                meta, source, classify_agent, refine_agent, followup_agent, api, deps
            )
        except Exception as exc:
            logger.exception("Triage raised for %s: %s", meta.id, exc)
            outcome = "error"
        counters[outcome] = counters.get(outcome, 0) + 1

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
    logger.info("Done: %s", summary)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the three-stage email triage pipeline (caddy-inbox)."
    )
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit (default when --loop is absent).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Minutes between runs when --loop is set (default: 15).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max emails processed per pass (default: 20).",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=14,
        help="notmuch date window (default: 14).",
    )
    parser.add_argument(
        "--backend",
        choices=["notmuch", "imap"],
        default=None,
        help="Override CADDY_EMAIL_BACKEND for this run.",
    )
    args = parser.parse_args()

    if args.loop:
        logger.info("Loop mode: every %d min.", args.interval)
        while True:
            try:
                await run_once(args.limit, args.backend, args.days_back)
            except Exception:
                logger.exception("run_once crashed — continuing.")
            await asyncio.sleep(args.interval * 60)
    else:
        await run_once(args.limit, args.backend, args.days_back)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
