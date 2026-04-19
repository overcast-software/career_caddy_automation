"""Full email-to-Career Caddy pipeline.

Searches for emails tagged 'job_post', extracts job URLs, scrapes each page,
and posts structured job data to the Career Caddy API.

Can run once or loop on an interval (useful for slow agents).

Usage:
    uv run caddy-email                  # single run
    uv run caddy-email --loop           # loop every 60 minutes
    uv run caddy-email --loop --interval 30  # loop every 30 minutes
    uv run caddy-email --url https://... # direct URL mode (skip email search)
"""

from lib.observability import configure_logfire
configure_logfire("caddy-email")

import os
import uuid
import logging
import json
import argparse
import asyncio
import time

from pydantic import BaseModel
from src.agents.caddy_poster import add_job_post
from src.agents.job_extractor import extract_job_from_content
from src.agents.agent_factory import get_model, get_model_name, get_agent, register_defaults
from src.agents.usage_reporter import report_usage
from pydantic_ai.usage import UsageLimits

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class JobOpportunity(BaseModel):
    """A job opportunity found in emails."""

    url: str
    title: str


register_defaults()


async def _scrape_url_and_add(url: str, api_token: str, pipeline_run_id: str) -> dict:
    """Scrape a single URL and add it to Career Caddy."""
    scraper_model = get_model("browser_scraper")
    scraper_agent = get_agent("browser_scraper")

    scrape_result = await scraper_agent.run(
        f"Scrape this URL and return all visible text: {url}",
        usage_limits=UsageLimits(request_limit=5),
    )

    if api_token:
        await report_usage(
            api_token=api_token,
            agent_name="browser_scraper",
            model_name=get_model_name(scraper_model),
            usage=scrape_result.usage(),
            trigger="pipeline",
            pipeline_run_id=pipeline_run_id,
        )

    raw_text = str(scrape_result.output or "")
    logger.info(f"Browser scrape output length: {len(raw_text)}")

    job_data = await extract_job_from_content(
        raw_text, url=url, api_token=api_token, pipeline_run_id=pipeline_run_id
    )

    logger.info(f"Extracted: {job_data.title} at {job_data.company_name}")

    caddy_result = await add_job_post(
        job_data, api_token=api_token, pipeline_run_id=pipeline_run_id
    )

    print(f"\n  Title: {job_data.title}")
    print(f"  Company: {job_data.company_name}")
    print(f"  Result: {caddy_result.get('action', 'unknown')}")

    return caddy_result


async def run_once(url: str | None = None):
    """Run the pipeline once — either for a specific URL or from email search."""
    pipeline_run_id = str(uuid.uuid4())
    api_token = os.environ.get("CC_API_TOKEN", "")

    if url:
        print(f"\n=== Direct URL Mode: {url} ===")
        await _scrape_url_and_add(url, api_token, pipeline_run_id)
        return

    # Step 1: Find job opportunities in emails
    logger.info("Step 1: Searching for job opportunities in emails...")
    _pipeline_model = get_model("pipeline")
    email_job_agent = get_agent(
        "pipeline",
        name="email_job_agent",
        output_type=list[JobOpportunity],
        system_prompt=(
            "Search for emails tagged 'job_post'. "
            "For each email found, read it and extract the job title and one primary job posting URL. "
            "Return a list of JobOpportunity objects. "
            "Only include URLs that point to actual job postings — skip unsubscribe links and tracking pixels."
        ),
    )

    email_result = await email_job_agent.run(
        "Search for emails tagged 'job_post'. "
        "Extract the job title and URL for each job posting found."
    )

    if api_token:
        await report_usage(
            api_token=api_token,
            agent_name="email_job_agent",
            model_name=get_model_name(_pipeline_model),
            usage=email_result.usage(),
            trigger="pipeline",
            pipeline_run_id=pipeline_run_id,
        )

    jobs = email_result.output
    print(f"\n=== Found {len(jobs)} Job Opportunities ===")
    for job in jobs:
        print(f"  {job.title}: {job.url}")

    # Step 2: Scrape and submit all jobs concurrently
    async def _process(job: JobOpportunity):
        logger.info(f"Processing: {job.title}")
        return await _scrape_url_and_add(job.url, api_token, pipeline_run_id)

    results = await asyncio.gather(
        *[_process(job) for job in jobs], return_exceptions=True
    )

    for job, result in zip(jobs, results):
        if isinstance(result, Exception):
            logger.error(f"Failed {job.title}: {result}")

    print(f"\n=== Complete: processed {len(jobs)} jobs ===")


async def main():
    parser = argparse.ArgumentParser(
        description="Email-to-Career Caddy pipeline"
    )
    parser.add_argument(
        "--url", type=str, help="Directly scrape a job URL (skip email search)"
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
                await run_once(url=args.url)
            except Exception as e:
                logger.error(f"Pipeline run failed: {e}")
            next_run = time.strftime("%H:%M", time.localtime(time.time() + args.interval * 60))
            print(f"\nSleeping {args.interval} minutes (next run ~{next_run})...")
            await asyncio.sleep(args.interval * 60)
    else:
        await run_once(url=args.url)


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
