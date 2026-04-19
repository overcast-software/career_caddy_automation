#!/usr/bin/env python3
"""Poll the Career Caddy API for hold scrapes, scrape locally, push results back.

The worker only runs the browser — extraction, job post creation, and scrape
profile updates are handled by the API when it receives the scraped content.

Usage:
    CC_API_BASE_URL=https://api.careercaddy.online \
    CC_API_TOKEN=jh_xxx \
    uv run caddy-poller
"""

from lib.observability import configure_logfire
configure_logfire("caddy-poller")

import asyncio
import json
import logging
import os
import signal
import sys

from src.client.api_client import ApiClient, get_scrapes, update_scrape
from mcp_servers.browser_server import scrape_page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("hold_poller")

POLL_INTERVAL = int(os.environ.get("HOLD_POLL_INTERVAL", "30"))


async def process_scrape(api: ApiClient, scrape: dict) -> bool:
    """Process a single hold scrape. Returns True on success."""
    scrape_id = int(scrape["id"])
    attrs = scrape.get("attributes", {})
    url = attrs.get("url")

    if not url:
        logger.warning("Scrape %s has no URL, skipping", scrape_id)
        await update_scrape(api, scrape_id, status="failed")
        return False

    logger.info("Processing scrape %s: %s", scrape_id, url)

    await update_scrape(api, scrape_id, status="running")

    try:
        result_json = await scrape_page(url)
        result = json.loads(result_json)

        if result.get("error") == "login_wall_detected":
            msg = result.get("message", "Login wall detected")
            logger.warning("Scrape %s: %s", scrape_id, msg)
            await update_scrape(api, scrape_id, status="failed")
            return False

        if result.get("error"):
            logger.error("Scrape %s error: %s", scrape_id, result["error"])
            await update_scrape(api, scrape_id, status="failed")
            return False

        content = result.get("content", "")
        if not content.strip():
            logger.warning("Scrape %s: empty content", scrape_id)
            await update_scrape(api, scrape_id, status="failed")
            return False

        screenshot_name = result.get("screenshot")
        if screenshot_name:
            from mcp_servers.browser_server import SCREENSHOT_DIR
            old_path = SCREENSHOT_DIR / screenshot_name
            new_name = f"scrape_{scrape_id}_{screenshot_name}"
            new_path = SCREENSHOT_DIR / new_name
            if old_path.exists():
                old_path.rename(new_path)
                logger.info("Screenshot: %s", new_path)

        await update_scrape(api, scrape_id, status="completed", job_content=content)
        logger.info("Scrape %s: content delivered (%d chars), API will extract", scrape_id, len(content))

        return True

    except Exception:
        logger.exception("Scrape %s failed", scrape_id)
        await update_scrape(api, scrape_id, status="failed")
        return False


async def poll_once(api: ApiClient) -> int:
    """Poll for hold scrapes and process them. Returns count processed."""
    raw = await get_scrapes(api, status="hold", sort="id")
    data = json.loads(raw)

    if not data.get("success"):
        logger.error("API error: %s", data.get("error"))
        return 0

    scrapes = data.get("data", {}).get("data", [])
    if not scrapes:
        return 0

    logger.info("Found %d hold scrape(s)", len(scrapes))

    processed = 0
    for scrape in scrapes:
        success = await process_scrape(api, scrape)
        if success:
            processed += 1

    return processed


async def main():
    base_url = os.environ.get("CC_API_BASE_URL")
    token = os.environ.get("CC_API_TOKEN")

    if not base_url or not token:
        logger.error("CC_API_BASE_URL and CC_API_TOKEN are required")
        sys.exit(1)

    api = ApiClient(base_url=base_url, token=token)

    running = True

    def stop(*_):
        nonlocal running
        running = False
        logger.info("Shutting down...")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    logger.info(
        "Hold poller started (interval=%ds, api=%s, headless=%s)",
        POLL_INTERVAL,
        base_url,
        os.environ.get("BROWSER_HEADLESS", "true"),
    )

    while running:
        count = await poll_once(api)
        if count:
            logger.info("Processed %d scrape(s)", count)
        await asyncio.sleep(POLL_INTERVAL)


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
