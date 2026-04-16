"""Classify and tag emails as job postings using an LLM agent.

Finds unevaluated emails via notmuch, classifies each with the email_classifier
agent, and tags job postings with 'job_post' + 'evaluated'.

Usage:
    uv run caddy-classify                  # single run
    uv run caddy-classify --loop           # loop every 60 minutes
    uv run caddy-classify --loop --interval 30
"""

import json
import os
import subprocess
import asyncio
import argparse
import time
import logging
from datetime import datetime, timedelta
from src.agents.usage_reporter import report_usage
from src.agents.agent_factory import get_model, get_model_name, get_agent, register_defaults

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an email classifier. You will be given a single email ID.

Your job:
1. Read the email using read_email(email_id)
2. Determine if it contains a job posting (job listing, recruiter outreach, job application link, etc.)
3. If it is a job posting: tag it with ["job_post", "evaluated"]
4. If it is NOT a job posting: tag it with ["evaluated"] only

Reply with one line: "job_post" or "not_job_post", followed by the email subject."""

register_defaults()
_classifier_model = get_model("email_classifier")

email_agent = get_agent(
    "email_classifier",
    system_prompt=SYSTEM_PROMPT,
)


def fetch_unevaluated_email_ids(limit: int = 20, days_back: int = 7) -> list[str]:
    """Fetch unevaluated email IDs directly from notmuch."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"date:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
    query = f"NOT tag:evaluated AND {date_range}"

    result = subprocess.run(
        ["notmuch", "search", "--format=json", f"--limit={limit}", query],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch failed: {result.stderr}")

    threads = json.loads(result.stdout)
    ids = []
    for thread in threads:
        query_arr = thread.get("query", [])
        if query_arr and query_arr[0]:
            email_id = query_arr[0]
            if email_id.startswith("id:"):
                email_id = email_id[3:]
            ids.append(email_id)
    return ids


async def classify_email(email_id: str) -> str:
    """Run a fresh isolated agent call for a single email."""
    result = await email_agent.run(f"Classify email id: {email_id}")

    api_token = os.environ.get("CC_API_TOKEN", "")
    if api_token:
        await report_usage(
            api_token=api_token,
            agent_name="email_classifier",
            model_name=get_model_name(_classifier_model),
            usage=result.usage(),
            trigger="classify",
        )

    return result.output


async def run_once():
    """Classify one batch of unevaluated emails."""
    email_ids = fetch_unevaluated_email_ids(limit=20)
    if not email_ids:
        print("No unevaluated emails found.")
        return

    print(f"Found {len(email_ids)} unevaluated emails. Classifying...\n")

    tagged = 0
    untagged = 0

    for email_id in email_ids:
        output = await classify_email(email_id)
        is_job = output.strip().lower().startswith("job_post")
        if is_job:
            tagged += 1
        else:
            untagged += 1
        print(f"{'[JOB]' if is_job else '[---]'} {output.strip()}")

    print(f"\nSummary: {tagged} job posts tagged, {untagged} not job posts")


async def main():
    parser = argparse.ArgumentParser(
        description="Classify and tag emails as job postings"
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously on an interval"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Minutes between runs when --loop is set (default: 60)"
    )
    args = parser.parse_args()

    if args.loop:
        print(f"=== Loop mode: running every {args.interval} minutes ===")
        while True:
            try:
                await run_once()
            except Exception as e:
                logger.error(f"Classification run failed: {e}")
            next_run = time.strftime("%H:%M", time.localtime(time.time() + args.interval * 60))
            print(f"\nSleeping {args.interval} minutes (next run ~{next_run})...")
            await asyncio.sleep(args.interval * 60)
    else:
        await run_once()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
