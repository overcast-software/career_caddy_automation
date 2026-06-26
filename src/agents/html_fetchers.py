"""HTML fetchers for the selector grounder.

Two strategies, same interface:

- StoredHtmlFetcher: pulls the HTML the scraper itself captured at failure
  time, via the Career Caddy MCP tool `fetch_scrape_html`. This is the
  ground truth — it matches the screenshot exactly. No network, no anti-bot.
- BrowserFetcher: full Camoufox/Playwright render of the page as it exists
  NOW. Reuses the session-store cookies + Firefox cookie import that the
  rest of the codebase's browser stack uses, so auth-gated pages are visible
  when the user has logged in via caddy-login. Use via --revisit when the
  stored HTML isn't available (older scrapes) or when you want current DOM.

The grounder only cares that it gets back the rendered HTML string — it
doesn't know which strategy produced it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class HtmlFetcher(Protocol):
    async def fetch(self, url: str) -> str | None: ...


def _unwrap_mcp(result: Any) -> Any:
    """fastmcp Client returns a CallToolResult; pull out the JSON payload."""
    if hasattr(result, "data") and result.data is not None:
        return result.data
    if hasattr(result, "content"):
        for part in result.content:
            text = getattr(part, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return result


class StoredHtmlFetcher:
    """Fetch the HTML column of a scrape record via the Career Caddy MCP.

    Returns None (and logs once) if the MCP server doesn't have
    `fetch_scrape_html` yet, or if the scrape has no stored HTML. Safe to
    wire in before the server-side tool ships — it just degrades to a miss."""

    _warned_missing_tool: bool = False

    def __init__(self, client: Any, scrape_id: str) -> None:
        self._client = client
        self._scrape_id = scrape_id

    async def fetch(self, url: str) -> str | None:  # noqa: ARG002  (url unused — scrape_id is key)
        try:
            result = await self._client.call_tool(
                "fetch_scrape_html",
                {"scrape_id": self._scrape_id},
            )
        except Exception as exc:
            # Tool not registered, or call-level error. Log once, then stay quiet.
            if not StoredHtmlFetcher._warned_missing_tool:
                logger.info(
                    "fetch_scrape_html unavailable on MCP server (%s) — "
                    "stored-HTML path disabled until the server ships it",
                    exc,
                )
                StoredHtmlFetcher._warned_missing_tool = True
            return None

        payload = _unwrap_mcp(result)
        if isinstance(payload, str):
            # Tool may return raw HTML directly.
            return payload or None
        if isinstance(payload, dict):
            if payload.get("success") is False:
                logger.info(
                    "fetch_scrape_html scrape=%s: %s",
                    self._scrape_id,
                    payload.get("error") or "no html",
                )
                return None
            for key in ("html", "content", "body", "data"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val
        logger.info(
            "fetch_scrape_html scrape=%s: empty payload shape %r",
            self._scrape_id,
            type(payload).__name__,
        )
        return None


class BrowserFetcher:
    """Camoufox/Playwright render. Loads saved session cookies when available
    so auth-gated pages come through logged-in (requires prior `caddy-login`)."""

    def __init__(self, headless: bool = True, timeout_ms: int = 30_000) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms

    async def fetch(self, url: str) -> str | None:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            logger.warning("camoufox not installed — install the 'browser' extra to use --revisit")
            return None

        cookies = self._load_cookies(url)
        try:
            async with AsyncCamoufox(headless=self._headless) as browser:
                ctx = await browser.new_context()
                if cookies:
                    try:
                        await ctx.add_cookies(cookies)
                    except Exception as exc:
                        logger.info("ignoring bad cookies for %s: %s", url, exc)
                page = await ctx.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                    return await page.content()
                finally:
                    await page.close()
                    await ctx.close()
        except Exception as exc:
            logger.warning("browser fetch %s failed: %s", url, exc)
            return None

    @staticmethod
    def _load_cookies(url: str) -> list[dict]:
        """Best-effort cookie load — session_store first, then Firefox import.
        Returns [] if nothing available; not fatal."""
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            return []
        if not host:
            return []
        try:
            from lib.browser import session_store
            from lib.browser.firefox_cookies import (
                _normalize_domain,
                load_cookies_for_domain,
            )
        except ImportError:
            return []
        try:
            norm = _normalize_domain(host)
            store = session_store.SessionStore()
            saved = store.load(norm) if norm else None
            if saved:
                logger.info("loaded %d saved cookie(s) for %s", len(saved), norm)
                return saved
            cookies = load_cookies_for_domain(host)
            if cookies:
                logger.info("loaded %d Firefox cookie(s) for %s", len(cookies), host)
            return cookies or []
        except Exception as exc:
            logger.info("cookie load for %s failed: %s", host, exc)
            return []
