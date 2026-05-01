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
import subprocess

from src.agents.email_agents import (
    FollowupResult,
    InlinePostResult,
    get_classify_agent,
    get_followup_agent,
    get_inline_post_agent,
    get_refine_agent,
)
from src.agents.url_extractor import extract_job_urls
from src.client.api_client import (
    ApiClient,
    create_job_post_minimal,
    create_job_post_with_company_check,
    get_job_applications,
    update_job_application,
)
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


async def _run_inline_post(agent, email_id: str) -> InlinePostResult:
    result = await agent.run(f"Extract inline JobPost from email id: {email_id}")
    return result.output


def _load_email_text(email_id: str) -> str:
    """Plain-text body of an email via `notmuch show`. Mirrors process_tagged."""
    result = subprocess.run(
        ["notmuch", "show", "--format=text", "--body=true", f"id:{email_id}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch show failed for {email_id}: {result.stderr.strip()}")
    return result.stdout


async def _create_posts_from_urls(
    api: ApiClient, urls, created_acc: list[dict] | None = None
) -> dict:
    """Create a JobPost per extracted URL. NO scrape is created — the user
    initiates scrapes from the UI on posts they want pulled.

    Outcome per URL is read from the api response:
      201 + new resource          → fresh create
      200 + existing post resource → api dedupe hit (link or fingerprint).
                                     `merge_empty_fields_from_attrs` ran on
                                     the existing post; response carries the
                                     post we mapped onto, including the
                                     api-computed `canonical_link`.
      4xx / non-success            → failed.
    """
    created: list[str] = []
    duplicates: list[str] = []
    failed: list[str] = []
    for link in urls:
        desc = link.description or None
        try:
            if link.company:
                raw = await create_job_post_with_company_check(
                    api,
                    title=link.title,
                    company_name=link.company,
                    link=link.url,
                    description=desc,
                    source="email",
                )
            else:
                raw = await create_job_post_minimal(
                    api,
                    title=link.title,
                    link=link.url,
                    description=desc,
                )
        except Exception as exc:
            logger.warning("  job-post raised for %s: %s", link.url, exc)
            failed.append(link.url)
            continue

        try:
            resp = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("  unparseable response for %s: %s", link.url, exc)
            failed.append(link.url)
            continue

        if not resp.get("success"):
            logger.warning("  job-post failed for %s: %s", link.url, resp.get("error"))
            failed.append(link.url)
            continue

        # JSON:API envelope: outer "data" wraps the inner resource at .data.data
        post_resource = (resp.get("data") or {}).get("data") or {}
        post_id = post_resource.get("id")
        attrs = post_resource.get("attributes") or {}
        canonical = attrs.get("canonical_link")
        status_code = resp.get("status_code")

        if status_code == 200:
            duplicates.append(link.url)
            logger.info(
                "  job-post dup: %s  id=%s  canonical=%s  (%s)",
                link.title, post_id, canonical, link.url,
            )
        else:
            # 201 (fresh create) — or any other 2xx the api evolves to use.
            created.append(link.url)
            logger.info(
                "  job-post: %s @ %s  id=%s  canonical=%s  (%s)",
                link.title, link.company or "—", post_id, canonical, link.url,
            )
            if created_acc is not None and post_id is not None:
                created_acc.append({
                    "id": post_id,
                    "title": link.title or "(untitled)",
                    "company": link.company or "—",
                    "link": canonical or link.url,
                    "source": "email_url",
                })
    return {"created": created, "duplicates": duplicates, "failed": failed}


async def _create_inline_job_post(
    api: ApiClient,
    res: InlinePostResult,
    created_acc: list[dict] | None = None,
) -> str | None:
    """POST a JobPost from an inline-JD email. Returns "created", "duplicate",
    or None on failure. link is null; source is "email_direct"."""
    description = res.description
    if res.recruiter_contact:
        description = f"Source: direct email from {res.recruiter_contact}\n\n{description}"
    try:
        if res.company:
            raw = await create_job_post_with_company_check(
                api,
                title=res.title,
                company_name=res.company,
                description=description,
                location=res.location,
                salary_min=res.salary_min,
                salary_max=res.salary_max,
                remote_ok=res.remote_ok,
                source="email_direct",
            )
        else:
            raw = await create_job_post_minimal(
                api,
                title=res.title,
                description=description,
                source="email_direct",
            )
        resp = json.loads(raw)
    except Exception as exc:
        logger.warning("  inline job-post raised: %s", exc)
        return None

    if (resp.get("data") or {}).get("duplicate"):
        return "duplicate"
    if resp.get("status_code") in (200, 409):
        return "duplicate"
    if not resp.get("success"):
        logger.warning("  inline job-post failed: %s", resp.get("error"))
        return None
    if created_acc is not None:
        post_resource = (resp.get("data") or {}).get("data") or {}
        post_id = post_resource.get("id")
        if post_id is not None:
            created_acc.append({
                "id": post_id,
                "title": res.title or "(untitled)",
                "company": res.company or "—",
                "link": None,
                "source": "email_direct",
            })
    return "created"


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
    inline_post_agent,
    api: ApiClient,
    deps: CareerCaddyDeps,
    created_acc: list[dict] | None = None,
) -> str:
    """Drive a single email through whichever stages it still needs. Returns
    a short status string for the summary counter.

    Logs a per-email outcome line in `finally` so every email — even the
    ones that fall through to "already_done" — produces one line that
    maps email_id → outcome → tags-added. Without it, the run summary
    "Done: already_done=2" reads as silence: you can't tell which email
    got the [FUP] tag this pass vs. which was a passive scan of an
    already-tagged thread.
    """
    email_id = meta.id
    initial_tags = set(meta.tags)
    tags = set(initial_tags)
    final_outcome = "already_done"
    try:
        # Stage 1 — classify (only if not yet evaluated).
        if "evaluated" not in tags:
            is_job = await _run_classify(classify_agent, email_id)
            new_tags = ["evaluated"] + (["job_post"] if is_job else [])
            await source.add_tags(email_id, new_tags)
            tags.update(new_tags)
            logger.info("[%s] %s  %s", "JOB" if is_job else "---", email_id, meta.subject)
            if not is_job:
                final_outcome = "not_job"
                return final_outcome

        # Stage 2 — refine (only job-related, only if not yet refined).
        if "job_post" in tags and "refined" not in tags:
            refined = await _run_refine(refine_agent, email_id)
            new_tags = ["refined"]
            confident = refined.confidence >= CONFIDENCE_FLOOR
            is_followup = refined.kind == "follow_up" and confident
            is_inline = refined.kind == "direct_solicitation" and confident
            if is_followup:
                new_tags.append("follow_up")
            if is_inline:
                new_tags.append("inline_post")
            await source.add_tags(email_id, new_tags)
            tags.update(new_tags)
            prefix = "FUP" if is_followup else ("DIR" if is_inline else "NEW")
            logger.info(
                "[%s] %s  conf=%.2f  %s",
                prefix,
                email_id,
                refined.confidence,
                refined.evidence[:80],
            )
            # Fall through to stage 5 for the new_post case (kind="new_post" or
            # low-confidence). The early-return that used to live here silently
            # dropped every job-board notification with a scrapeable URL.

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
                    tags.add("caddy_processed")
                    final_outcome = "processed"
                    return final_outcome
                final_outcome = "update_failed"
                return final_outcome
            logger.info(
                "  no confident application match for %s (conf=%.2f): %s",
                email_id,
                res.confidence,
                res.notes[:120],
            )
            final_outcome = "unmatched"
            return final_outcome

        # Stage 4 — inline-post extractor (direct-solicitation emails: JD inline,
        # no scrapeable URL). Creates a JobPost with link=NULL and
        # source="email_direct" so the post is distinguishable from URL-scraped
        # email-sourced posts.
        if "inline_post" in tags and "caddy_processed" not in tags:
            res = await _run_inline_post(inline_post_agent, email_id)
            if not res.title or res.confidence < CONFIDENCE_FLOOR:
                logger.info(
                    "  inline-post low confidence for %s (conf=%.2f, title=%r): %s",
                    email_id,
                    res.confidence,
                    res.title,
                    res.evidence[:120],
                )
                final_outcome = "inline_unmatched"
                return final_outcome
            inline_outcome = await _create_inline_job_post(api, res, created_acc)
            if inline_outcome is None:
                final_outcome = "inline_failed"
                return final_outcome
            await source.add_tags(email_id, ["caddy_processed"])
            tags.add("caddy_processed")
            logger.info(
                "  inline-post %s: %s @ %s  conf=%.2f",
                inline_outcome,
                res.title,
                res.company or "—",
                res.confidence,
            )
            final_outcome = f"inline_{inline_outcome}"
            return final_outcome

        # Stage 5 — URL-extract → create JobPost(s) for the default new_post case
        # (a job-board notification with one or more scrapeable URLs, neither a
        # follow-up correspondence nor an inline-JD recruiter pitch). NO scrape is
        # created — the user initiates a scrape from the UI on posts they want
        # pulled. This stage was the missing piece between caddy-classify+caddy-
        # process (legacy two-daemon flow) and caddy-inbox (orchestrator); the
        # refiner correctly tagged emails `new_post` but nothing acted on it.
        if (
            "refined" in tags
            and "follow_up" not in tags
            and "inline_post" not in tags
            and "caddy_processed" not in tags
        ):
            try:
                text = _load_email_text(email_id)
            except RuntimeError as exc:
                logger.warning("  stage5: load_email_text failed for %s: %s", email_id, exc)
                final_outcome = "new_load_failed"
                return final_outcome
            extracted = await extract_job_urls(text)
            if not extracted.job_urls:
                await source.add_tags(email_id, ["caddy_processed"])
                tags.add("caddy_processed")
                logger.info(
                    "  stage5: no URLs extracted from %s (%s)",
                    email_id,
                    extracted.reasoning[:120],
                )
                final_outcome = "new_no_urls"
                return final_outcome
            url_outcome = await _create_posts_from_urls(api, extracted.job_urls, created_acc)
            if not url_outcome["failed"]:
                await source.add_tags(email_id, ["caddy_processed"])
                tags.add("caddy_processed")
            logger.info(
                "  stage5: created=%d duplicates=%d failed=%d",
                len(url_outcome["created"]),
                len(url_outcome["duplicates"]),
                len(url_outcome["failed"]),
            )
            if url_outcome["failed"]:
                final_outcome = "new_failed"
                return final_outcome
            if url_outcome["created"]:
                final_outcome = "new_created"
                return final_outcome
            final_outcome = "new_duplicate"
            return final_outcome

        final_outcome = "already_done"
        return final_outcome
    finally:
        added = sorted(tags - initial_tags)
        diff = ",".join(added) if added else "—"
        logger.info(
            "  → %-18s %s  added=[%s]  %s",
            final_outcome,
            email_id,
            diff,
            (meta.subject or "(no subject)")[:70],
        )


async def run_once(limit: int, backend: str | None, days_back: int) -> None:
    source = make_source(backend)
    pending = await source.list_pending(limit=limit, days_back=days_back)
    if not pending:
        logger.info("No pending emails.")
        return

    classify_agent = get_classify_agent()
    refine_agent = get_refine_agent()
    followup_agent = get_followup_agent()
    inline_post_agent = get_inline_post_agent()
    api = _api_client()
    deps = _caddy_deps()

    counters: dict[str, int] = {}
    created_acc: list[dict] = []
    for meta in pending:
        try:
            outcome = await _triage_one(
                meta,
                source,
                classify_agent,
                refine_agent,
                followup_agent,
                inline_post_agent,
                api,
                deps,
                created_acc=created_acc,
            )
        except Exception as exc:
            logger.exception("Triage raised for %s: %s", meta.id, exc)
            outcome = "error"
        counters[outcome] = counters.get(outcome, 0) + 1

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
    logger.info("Done: %s", summary)
    if created_acc:
        logger.info("Created %d JobPost(s) this pass:", len(created_acc))
        for post in created_acc:
            logger.info(
                "  jp #%s [%s]  %s @ %s  %s",
                post["id"],
                post["source"],
                post["title"][:60],
                post["company"],
                post["link"] or "(no link)",
            )


# State queries used by --status. Same date scope as list_pending so
# counts line up with what the daemon would see on a normal pass.
STATE_QUERIES: dict[str, str] = {
    "unevaluated":             "not tag:evaluated",
    "evaluated_not_job":       "tag:evaluated and not tag:job_post",
    "job_post_pending_refine": "tag:job_post and not tag:refined",
    "refined_follow_up":       "tag:follow_up and not tag:caddy_processed",
    "refined_inline_post":     "tag:inline_post and not tag:caddy_processed",
    "refined_new_post":        "tag:refined and not tag:follow_up and not tag:inline_post and not tag:caddy_processed",
    "caddy_processed":         "tag:caddy_processed",
}


async def print_status(backend: str | None, days_back: int, show: str | None, show_limit: int) -> None:
    """Tag-state breakdown of the mailbox so the user can see where
    pending work is stuck without watching live logs. `--show <state>`
    dumps the matching email subjects/ids."""
    source = make_source(backend)
    if not hasattr(source, "count_by_query"):
        raise RuntimeError(
            f"--status not supported for backend {type(source).__name__}; "
            "only NotmuchSource implements count_by_query so far."
        )

    if show is not None:
        if show not in STATE_QUERIES:
            valid = ", ".join(STATE_QUERIES.keys())
            raise SystemExit(f"--show: unknown state {show!r}. Valid: {valid}")
        metas = await source.list_by_query(
            STATE_QUERIES[show], limit=show_limit, days_back=days_back
        )
        logger.info("=== %s (showing %d, last %d days) ===", show, len(metas), days_back)
        for m in metas:
            tag_str = ",".join(sorted(m.tags)) or "(none)"
            logger.info("  %s  [%s]  %s", m.id, tag_str, (m.subject or "")[:80])
        return

    logger.info("=== Pipeline state (last %d days) ===", days_back)
    width = max(len(k) for k in STATE_QUERIES)
    for state, query in STATE_QUERIES.items():
        n = await source.count_by_query(query, days_back=days_back)
        logger.info("  %-*s : %4d", width, state, n)
    logger.info("(use --show <state> to list matching emails)")


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
    parser.add_argument(
        "--status",
        action="store_true",
        help=(
            "Print a tag-state breakdown of the mailbox and exit. Use to "
            "find emails stuck mid-pipeline (e.g., 'evaluated_not_job' = "
            "candidates the classifier rejected; 'refined_follow_up' = "
            "follow-ups not yet matched to an application)."
        ),
    )
    parser.add_argument(
        "--show",
        type=str,
        default=None,
        metavar="STATE",
        help=(
            "With --status: list the matching email ids/subjects for the "
            "named state (one of: " + ", ".join(STATE_QUERIES.keys()) + ")."
        ),
    )
    parser.add_argument(
        "--show-limit",
        type=int,
        default=20,
        help="Max emails listed by --show (default: 20).",
    )
    args = parser.parse_args()

    if args.status or args.show is not None:
        await print_status(args.backend, args.days_back, args.show, args.show_limit)
        return

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
