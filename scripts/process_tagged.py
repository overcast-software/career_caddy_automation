"""
Process tagged job emails: extract job URLs + context, create lightweight
job-posts in Career Caddy.

For each email tagged `job_post` and not yet `caddy_processed`:
  1. Dump its body (with headers) via `notmuch show`
  2. Ask the url_extractor agent for {url, title, company, description}
     per real job listing
  3. POST each one as a job-post — with company check when the company
     is known, minimal (no company) otherwise. Idempotent on the `link`
     field, so reruns are safe.
  4. Tag the email `caddy_processed`

Scrapes are NOT created here — users trigger them from the Career Caddy
frontend on posts they're interested in.

Usage:
    uv run caddy-process                      # loop every 60 minutes
    uv run caddy-process --once               # single run
    uv run caddy-process --limit 5            # process up to 5 emails per run
    uv run caddy-process --interval 30        # loop every 30 minutes
"""

from lib.observability import configure_logfire

configure_logfire("caddy-process")

import argparse
import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime

from src.agents.span_validator import filter_span_atomic
from src.agents.url_extractor import extract_job_urls
from src.client.api_client import (
    ApiClient,
    create_job_post_minimal,
    create_job_post_with_company_check,
    create_scrape,
    get_scrapes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def fetch_pending_emails(limit: int) -> list[dict]:
    """Query notmuch for job_post emails not yet caddy_processed."""
    result = subprocess.run(
        [
            "notmuch",
            "search",
            "--format=json",
            f"--limit={limit}",
            "tag:job_post AND NOT tag:follow_up AND NOT tag:caddy_processed",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch failed: {result.stderr.strip()}")

    emails = []
    for thread in json.loads(result.stdout):
        query_arr = thread.get("query", [])
        if not query_arr or not query_arr[0]:
            continue
        raw_id = query_arr[0]
        email_id = raw_id[3:] if raw_id.startswith("id:") else raw_id
        emails.append(
            {
                "email_id": email_id,
                "subject": thread.get("subject", "(no subject)"),
                "authors": thread.get("authors", ""),
                "date_relative": thread.get("date_relative", ""),
            }
        )
    return emails


def load_email_text(email_id: str) -> str:
    """Return the plain-text body of an email via `notmuch show`."""
    result = subprocess.run(
        ["notmuch", "show", "--format=text", "--body=true", f"id:{email_id}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch show failed for {email_id}: {result.stderr.strip()}")
    return result.stdout


def tag_processed(email_id: str) -> None:
    subprocess.run(
        ["notmuch", "tag", "+caddy_processed", f"id:{email_id}"],
        check=True,
        timeout=10,
    )


def _api_client() -> ApiClient:
    token = os.environ["CC_API_TOKEN"]
    base_url = os.environ.get("CC_API_BASE_URL", "http://localhost:8000")
    return ApiClient(base_url, token)


def _auto_scrape_enabled() -> bool:
    return os.environ.get("CADDY_AUTO_SCRAPE", "").strip().lower() in {"1", "true", "yes", "on"}


def _post_id_from_response(resp: dict) -> str | None:
    data = resp.get("data") or {}
    if isinstance(data, dict) and data.get("duplicate") and data.get("existing_id"):
        return str(data["existing_id"])
    inner = data.get("data") if isinstance(data, dict) else None
    if isinstance(inner, dict) and inner.get("id") is not None:
        return str(inner["id"])
    return None


async def _ensure_hold_scrape(api: ApiClient, job_post_id: str, url: str) -> str | None:
    """Create a hold-status scrape for this job-post if none exists. Returns status tag."""
    try:
        existing_raw = await get_scrapes(api, job_post_id=job_post_id, per_page=1)
        existing = json.loads(existing_raw)
        if existing.get("success"):
            rows = (existing.get("data") or {}).get("data") or []
            if rows:
                return "exists"
    except Exception as exc:
        logger.warning("  scrape lookup failed for job_post %s: %s", job_post_id, exc)

    try:
        raw = await create_scrape(api, url=url, job_post_id=job_post_id, status="hold")
        resp = json.loads(raw)
        if resp.get("success"):
            return "created"
        logger.warning("  scrape create failed for job_post %s: %s", job_post_id, resp.get("error"))
    except Exception as exc:
        logger.warning("  scrape create raised for job_post %s: %s", job_post_id, exc)
    return None


async def process_single_email(email_id: str, api: ApiClient) -> dict:
    logger.info("Processing email: %s", email_id)
    try:
        text = load_email_text(email_id)
        extracted = await extract_job_urls(text)
        before_span = len(extracted.job_urls)
        extracted.job_urls = filter_span_atomic(
            extracted.job_urls,
            text,
            email_id=email_id,
        )
        if len(extracted.job_urls) != before_span:
            logger.info(
                "  span_validator dropped %d/%d url(s) (cross-row hallucination guard)",
                before_span - len(extracted.job_urls),
                before_span,
            )
        logger.info("  extractor kept %d url(s): %s", len(extracted.job_urls), extracted.reasoning)

        created: list[str] = []
        duplicates: list[str] = []
        failed: list[tuple[str, str]] = []
        scrapes_created = 0
        auto_scrape = _auto_scrape_enabled()
        for link in extracted.job_urls:
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
                    # create_job_post_minimal defaults source='email' already.
                    raw = await create_job_post_minimal(
                        api,
                        title=link.title,
                        link=link.url,
                        description=desc,
                    )
            except Exception as exc:
                logger.warning("  job-post raised for %s: %s", link.url, exc)
                failed.append((link.url, str(exc)))
                continue

            try:
                resp = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("  unparseable response for %s: %s", link.url, exc)
                failed.append((link.url, f"unparseable response: {exc}"))
                continue

            # API returns 201 on a fresh create and 200 when the link/fingerprint
            # already exists (echoes the canonical record). 409 + data.duplicate
            # are kept for forward-compat with explicit-conflict responses.
            is_duplicate = (
                resp.get("status_code") in (200, 409)
                or (resp.get("data") or {}).get("duplicate") is True
            )
            if is_duplicate:
                duplicates.append(link.url)
                logger.info("  job-post dup: %s  (%s)", link.title, link.url)
            elif not resp.get("success"):
                err = resp.get("error", "unknown error")
                logger.warning("  job-post failed for %s: %s", link.url, err)
                failed.append((link.url, str(err)))
                continue
            else:
                created.append(link.url)
                logger.info(
                    "  job-post: %s @ %s  desc=%s  (%s)",
                    link.title,
                    link.company or "—",
                    "yes" if link.description else "no",
                    link.url,
                )

            if auto_scrape:
                post_id = _post_id_from_response(resp)
                if post_id is None:
                    logger.warning("  auto-scrape: could not extract job_post id for %s", link.url)
                else:
                    outcome = await _ensure_hold_scrape(api, post_id, link.url)
                    if outcome == "created":
                        scrapes_created += 1
                        logger.info(
                            "  scrape: hold created for job_post %s (%s)", post_id, link.url
                        )
                    elif outcome == "exists":
                        logger.info("  scrape: already present for job_post %s", post_id)

        if failed:
            return {
                "email_id": email_id,
                "success": False,
                "kept": len(extracted.job_urls),
                "created": len(created),
                "duplicates": len(duplicates),
                "error": f"{len(failed)}/{len(extracted.job_urls)} post(s) failed; leaving untagged for retry",
            }

        tag_processed(email_id)
        return {
            "email_id": email_id,
            "success": True,
            "kept": len(extracted.job_urls),
            "created": len(created),
            "duplicates": len(duplicates),
            "scrapes_created": scrapes_created,
            "reasoning": extracted.reasoning,
        }
    except Exception as exc:
        logger.exception("Failed to process email %s", email_id)
        return {"email_id": email_id, "success": False, "error": str(exc)}


async def run_once(limit: int = 3) -> str:
    logger.info("Starting caddy-process run (limit=%d)", limit)
    pending = fetch_pending_emails(limit)
    logger.info("Found %d pending email(s)", len(pending))
    for i, em in enumerate(pending, 1):
        logger.info("  %d. [%s] %s  <%s>", i, em["date_relative"], em["subject"], em["authors"])

    if not pending:
        return "No pending emails."

    api = _api_client()
    results = []
    for em in pending:
        r = await process_single_email(em["email_id"], api)
        r["subject"] = em["subject"]
        results.append(r)

    ok = sum(1 for r in results if r.get("success"))
    fail = len(results) - ok
    total_posts = sum(r.get("created", 0) for r in results)

    lines = [
        f"Run complete: {len(results)} email(s), {ok} ok, {fail} failed, {total_posts} job-post(s) created"
    ]
    for r in results:
        if r.get("success"):
            lines.append(
                f"  [ok  ] {r.get('subject', '?')}  →  "
                f"{r['created']} new, {r.get('duplicates', 0)} dup / {r['kept']} kept"
                + (f", {r['scrapes_created']} hold scrape(s)" if r.get("scrapes_created") else "")
            )
        else:
            lines.append(f"  [FAIL] {r.get('subject', '?')}  →  {r.get('error', '?')}")
    summary = "\n".join(lines)
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
    subprocess.run(["notmuch", "new"], check=False)
    parser = argparse.ArgumentParser(
        description="Extract job URLs from tagged emails and push them to Career Caddy as scrapes."
    )
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit")
    parser.add_argument(
        "--limit", type=int, default=3, metavar="N", help="Max emails per run (default: 3)"
    )
    parser.add_argument(
        "--interval", type=int, default=60, metavar="MINUTES", help="Loop interval (default: 60)"
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
