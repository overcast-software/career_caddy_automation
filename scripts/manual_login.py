#!/usr/bin/env python3
"""
Open a headed browser, log in manually to each domain,
then save all session cookies when you're done.

Sessions are stored in ~/.career_caddy/sessions/ and shared with
the browser_server MCP tool (scrape_page, navigate, etc.).

Usage:
    uv run python scripts/manual_login.py                       # opens about:blank
    uv run python scripts/manual_login.py monster.com dice.com  # opens each domain
    uv run caddy-login monster.com dice.com                     # via entry point
"""

import asyncio
import sys

from camoufox.async_api import AsyncCamoufox
from lib.browser.credentials import Credentials
from lib.browser.session_store import SessionStore


async def main(domains: list[str]) -> None:
    session_store = SessionStore()

    print("Starting browser...")
    async with AsyncCamoufox(headless=False) as browser:
        ctx = await browser.new_context()

        if domains:
            for domain in domains:
                url = f"https://{domain}"
                page = await ctx.new_page()
                await page.goto(url, wait_until="commit", timeout=60_000)
                print(f"  Opened {url}")
        else:
            page = await ctx.new_page()
            await page.goto("about:blank")
            print("  Opened blank tab")

        print()
        print("Log in to each site in the browser.")
        print("When you're done, press Enter here to save sessions and exit.")
        await asyncio.get_event_loop().run_in_executor(None, input, ">>> Press Enter when done: ")

        # Collect all cookies and bucket them by normalized domain
        all_cookies = await ctx.cookies()
        by_domain: dict[str, list[dict]] = {}
        for cookie in all_cookies:
            raw = cookie.get("domain", "").lstrip(".")
            if not raw:
                continue
            norm = Credentials.normalize_domain(raw)
            by_domain.setdefault(norm, []).append(cookie)

        if not by_domain:
            print("No cookies found — nothing saved.")
            return

        for domain, cookies in sorted(by_domain.items()):
            session_store.save(domain, cookies)
            print(f"  Saved {len(cookies):3d} cookies for {domain}")

        print(f"\nDone. Sessions saved to {session_store.sessions_dir}")


def run():
    targets = [Credentials.normalize_domain(d) for d in sys.argv[1:]]
    asyncio.run(main(targets))


if __name__ == "__main__":
    run()
