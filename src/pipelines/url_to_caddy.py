"""Scrape a job URL and add it to Career Caddy.

The simplest pipeline: given a URL, scrape the page, extract structured
job data, and post it to the Career Caddy API.

Usage:
    uv run caddy-url https://example.com/job/posting
"""

from lib.observability import configure_logfire
configure_logfire("caddy-url")

import os
import uuid
import logging
import json
import argparse
import asyncio

from src.agents.caddy_poster import add_job_post
from src.agents.job_extractor import extract_job_from_content
from src.agents.agent_factory import get_model, get_model_name, get_agent, register_defaults
from src.agents.usage_reporter import report_usage
from pydantic_ai.usage import UsageLimits

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

register_defaults()


async def scrape_url_and_add_to_caddy(url: str, pipeline_run_id: str | None = None) -> dict:
    """Scrape a job URL and add it to Career Caddy."""
    logger.info(f"Scraping job URL: {url}")
    run_id = pipeline_run_id or str(uuid.uuid4())
    api_token = os.environ.get("CC_API_TOKEN", "")

    # Scrape the page via browser agent
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
            pipeline_run_id=run_id,
        )

    raw_text = str(scrape_result.output or "")
    logger.info(f"Browser scrape output length: {len(raw_text)}")

    # Extract structured job data
    job_data = await extract_job_from_content(
        raw_text, url=url, api_token=api_token, pipeline_run_id=run_id
    )

    logger.info(f"Extracted job data: {job_data.title} at {job_data.company_name}")

    # Add to Career Caddy
    caddy_result = await add_job_post(
        job_data, api_token=api_token, pipeline_run_id=run_id
    )

    print("\n=== Added Job Post to Career Caddy ===")
    print(f"Title: {job_data.title}")
    print(f"Company: {job_data.company_name}")
    print(f"Location: {job_data.location}")
    print(f"URL: {job_data.url}")
    print(f"\nCareer Caddy Response:")
    print(json.dumps(caddy_result, indent=2))

    return caddy_result


async def main():
    parser = argparse.ArgumentParser(
        description="Scrape a job URL and add it to Career Caddy"
    )
    parser.add_argument("url", type=str, help="Job posting URL to scrape")
    args = parser.parse_args()

    await scrape_url_and_add_to_caddy(args.url)


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
