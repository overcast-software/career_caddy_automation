"""Score job-posts whose scrape has completed.

Polls Career Caddy for scrapes in ``status=completed`` whose linked
job-post has no ``Score`` yet, then calls ``score_job_post`` for each.
Scoring is an expensive LLM operation on the server; this daemon keeps
it isolated so it can be toggled off by simply not running it.

Usage:
    uv run caddy-score                 # loop every 30 minutes
    uv run caddy-score --once          # single run
    uv run caddy-score --limit 5       # max posts scored per run
    uv run caddy-score --interval 15   # minutes between runs
"""

from lib.observability import configure_logfire

configure_logfire("caddy-score")

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime

import logfire

from src.client.api_client import (
    ApiClient,
    get_scores,
    get_scrapes,
    score_job_post,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _api_client() -> ApiClient:
    token = os.environ["CC_API_TOKEN"]
    base_url = os.environ.get("CC_API_BASE_URL", "http://localhost:8000")
    return ApiClient(base_url, token)


def _job_post_id_from_scrape(row: dict) -> int | None:
    rels = row.get("relationships") or {}
    jp = (rels.get("job-post") or rels.get("job_post") or {}).get("data") or {}
    raw = jp.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _already_scored(api: ApiClient, job_post_id: int) -> bool:
    try:
        raw = await get_scores(api, job_post_id=job_post_id, per_page=1)
        resp = json.loads(raw)
    except Exception as exc:
        logger.warning("  score lookup failed for job_post %s: %s", job_post_id, exc)
        return False
    if not resp.get("success"):
        return False
    rows = (resp.get("data") or {}).get("data") or []
    return bool(rows)


async def _collect_candidates(api: ApiClient, limit: int) -> list[int]:
    """Return job_post ids whose scrape completed but have no score yet."""
    try:
        raw = await get_scrapes(
            api,
            status="completed",
            sort="-scraped_at",
            per_page=limit * 4,
        )
        resp = json.loads(raw)
    except Exception as exc:
        logger.error("Scrape query failed: %s", exc)
        return []

    if not resp.get("success"):
        logger.warning("Scrape query unsuccessful: %s", resp.get("error"))
        return []

    rows = (resp.get("data") or {}).get("data") or []
    out: list[int] = []
    seen: set[int] = set()
    for row in rows:
        post_id = _job_post_id_from_scrape(row)
        if post_id is None or post_id in seen:
            continue
        seen.add(post_id)
        if await _already_scored(api, post_id):
            continue
        out.append(post_id)
        if len(out) >= limit:
            break
    return out


async def score_one(api: ApiClient, job_post_id: int) -> bool:
    with logfire.span("score_posts.score_one", job_post_id=job_post_id):
        try:
            raw = await score_job_post(api, job_post_id)
            resp = json.loads(raw)
        except Exception:
            logger.exception("Scoring raised for job_post %s", job_post_id)
            return False
        if not resp.get("success"):
            logger.warning("Scoring failed for job_post %s: %s", job_post_id, resp.get("error"))
            return False
        logger.info("  scored job_post %s", job_post_id)
        return True


async def run_once(limit: int = 5) -> str:
    logger.info("Starting caddy-score run (limit=%d)", limit)
    api = _api_client()
    candidates = await _collect_candidates(api, limit=limit)
    logger.info("Found %d post(s) eligible for scoring", len(candidates))
    if not candidates:
        return "No posts to score."

    ok = 0
    for post_id in candidates:
        if await score_one(api, post_id):
            ok += 1

    summary = f"Scored {ok}/{len(candidates)} job-post(s)"
    logger.info("%s", summary)
    return summary


async def loop(interval_minutes: int, limit: int) -> None:
    while True:
        start = datetime.now()
        try:
            await run_once(limit=limit)
        except Exception:
            logger.exception("Run failed")
        elapsed = (datetime.now() - start).total_seconds()
        sleep_secs = max(0, interval_minutes * 60 - elapsed)
        logger.info("Next run in %.0f minutes", sleep_secs / 60)
        try:
            await asyncio.sleep(sleep_secs)
        except asyncio.CancelledError:
            break


def run():
    parser = argparse.ArgumentParser(description="Score job-posts whose scrape has completed.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit")
    parser.add_argument(
        "--limit", type=int, default=5, metavar="N", help="Max posts per run (default: 5)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        metavar="MINUTES",
        help="Loop interval (default: 30)",
    )
    args = parser.parse_args()

    try:
        if args.once:
            asyncio.run(run_once(limit=args.limit))
        else:
            asyncio.run(loop(args.interval, limit=args.limit))
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting.")


if __name__ == "__main__":
    run()
