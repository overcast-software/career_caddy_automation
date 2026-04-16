"""
Process tagged job emails and add them to Career Caddy via the A2A orchestrator.

Finds emails tagged 'job_post' but not 'caddy_processed', classifies each
(new posting, interview, rejection, offer, etc.), and takes the appropriate
action via the orchestrator's sub-agents.

Usage:
    uv run caddy-process                      # single run
    uv run caddy-process --loop               # loop every 60 minutes
    uv run caddy-process --loop --interval 30 # loop every 30 minutes
    uv run caddy-process --limit 5            # process up to 5 emails per run
"""

import argparse
import asyncio
import json
import logging
import subprocess
from datetime import datetime

from src.agents.a2a_orchestrator import a2a_orchestrator

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
            "notmuch", "search", "--format=json",
            f"--limit={limit}",
            "tag:job_post AND NOT tag:caddy_processed",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch failed: {result.stderr.strip()}")

    threads = json.loads(result.stdout)
    emails = []
    for thread in threads:
        query_arr = thread.get("query", [])
        if not query_arr or not query_arr[0]:
            continue
        raw_id = query_arr[0]
        email_id = raw_id[3:] if raw_id.startswith("id:") else raw_id
        emails.append({
            "email_id": email_id,
            "subject": thread.get("subject", "(no subject)"),
            "authors": thread.get("authors", ""),
            "date_relative": thread.get("date_relative", ""),
        })
    return emails


PROCESS_EMAIL_PROMPT_TEMPLATE = """
Read email ID {email_id} using the email agent, then take the appropriate action in Career Caddy.

## Step 1 — Classify the email

Read the email and determine which category it falls into:

A) **New job posting** — contains a direct link to a specific job listing page
B) **Interview invite** — recruiter or employer inviting me to interview for a role I applied to
C) **Rejection** — employer or recruiter notifying me that my application was not selected
D) **Offer** — employer extending a job offer
E) **Other job correspondence** — any other reply or update about a job application
   (e.g. application received confirmation, request for more info, follow-up, etc.)
F) **Not actionable** — newsletter, marketing, unsubscribe confirmation, tracking pixel,
   recruiter cold-outreach with no specific role, or anything not related to an active application.
   Skip entirely.

## Step 2 — Act based on category

### Category A — New job posting
1. Extract valid job posting URLs (http/https only; must look like a job page — skip image URLs,
   CDN assets, tracking pixels, unsubscribe links, logos, company homepages).
2. For each URL (max 3):
   a. Check for duplicates via `find_job_post_by_link`
   b. If not duplicate: use the browser agent to scrape the job details, then
      `create_job_post_with_company_check` in Career Caddy.

### Categories B, C, D, E — Application update
1. Extract the company name, job title, and any job URL from the email.
2. Call the caddy agent with full context — e.g.:
   "Find the job application for company='Acme Corp' role='Senior SWE'
    (URL if available: https://...) and update its status to 'interviewing'."
   The caddy agent is stateless, so include all extracted details in the request.
3. Status mapping:
   - Interview invite  → status="interviewing"
   - Rejection         → status="rejected"
   - Offer             → status="offered"
   - Other             → append notes with a brief summary of the email
4. If no matching application is found, note it but do not create a new one.

## Step 3 — Report
Return a one-line summary: category, company/role if known, and what action was taken (or skipped).

Do NOT tag the email — that is handled separately.
"""

TAG_EMAIL_PROMPT_TEMPLATE = """
Use the email agent to tag email ID {email_id} with the tag 'caddy_processed'.
Confirm when done.
"""


async def process_single_email(email_id: str) -> dict:
    """Process a single email. Returns summary dict."""
    logger.info(f"Processing email: {email_id}")
    try:
        prompt = PROCESS_EMAIL_PROMPT_TEMPLATE.format(email_id=email_id)
        result = await a2a_orchestrator.run(prompt)
        summary = str(result.output)
        logger.info(f"Email {email_id} processed: {summary}")

        tag_prompt = TAG_EMAIL_PROMPT_TEMPLATE.format(email_id=email_id)
        await a2a_orchestrator.run(tag_prompt)
        logger.info(f"Email {email_id} tagged as caddy_processed")

        return {"email_id": email_id, "success": True, "summary": summary}
    except Exception as e:
        logger.error(f"Failed to process email {email_id}: {e}")
        return {"email_id": email_id, "success": False, "error": str(e)}


async def run_once(limit: int = 3) -> str:
    """Run one processing pass. Returns a summary string."""
    logger.info("Starting job hunt email processing run (limit=%d)", limit)

    logger.info("Querying notmuch: tag:job_post AND NOT tag:caddy_processed")
    pending = fetch_pending_emails(limit)
    logger.info("Found %d pending email(s):", len(pending))
    for i, em in enumerate(pending, 1):
        logger.info("  %d. [%s] %s  <%s>", i, em["date_relative"], em["subject"], em["authors"])

    if not pending:
        summary = "No pending emails (tag:job_post AND NOT tag:caddy_processed)."
        logger.info(summary)
        return summary

    results = []
    for em in pending:
        result = await process_single_email(em["email_id"])
        result["subject"] = em["subject"]
        result["authors"] = em["authors"]
        results.append(result)

    successful = sum(1 for r in results if r.get("success"))
    failed = len(results) - successful

    lines = [
        f"Processing complete: {len(results)} email(s), {successful} ok, {failed} failed",
    ]
    for r in results:
        status = "ok" if r.get("success") else f"FAIL: {r.get('error', '?')}"
        lines.append(f"  [{status}] {r.get('subject', r['email_id'])}  <{r.get('authors', '')}>")
        if r.get("summary"):
            lines.append(f"         → {r['summary']}")

    summary = "\n".join(lines)
    logger.info("Run complete:\n%s", summary)
    return summary


async def loop(interval_minutes: int, limit: int) -> None:
    """Run processing once immediately, then repeat every interval_minutes."""
    while True:
        start = datetime.now()
        try:
            await run_once(limit=limit)
        except Exception as e:
            logger.error("Processing run failed: %s", e)

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
        description="Process tagged job emails and add to Career Caddy via A2A orchestrator"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single processing pass and exit",
    )
    parser.add_argument(
        "--limit", type=int, default=3, metavar="N",
        help="Max emails to process per run (default: 3)",
    )
    parser.add_argument(
        "--interval", type=int, default=60, metavar="MINUTES",
        help="Loop interval in minutes (default: 60)",
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
