#!/usr/bin/env python3
"""Analyze screenshots of failed scrapes for a domain; suggest ScrapeProfile
improvements and optionally write safe fields back.

Connects to the Career Caddy public MCP server (mcp.careercaddy.online/mcp)
using the user's jh_* API key. Staff-level keys are required because the
screenshot endpoints are staff-only.

Write policy:
  - Auto-written when --write is set: css_selectors.analyzer_notes (audit
    log) and css_selectors.interaction_hints (free-text, deduplicated,
    capped at 2000 chars).
  - Propose-only: ready_selector, obstacle_click_selector. Printed to stdout
    so a human can apply them.

Usage:
    CC_API_TOKEN=jh_xxx \\
    uv run caddy-analyze-screenshots --domain linkedin.com --limit 5 [--write]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastmcp import Client

from src.agents.screenshot_analyzer import analyze_screenshot, ScreenshotAnalysis

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("analyze_screenshots")

DEFAULT_MCP_URL = "https://mcp.careercaddy.online/mcp"
INTERACTION_HINTS_CAP = 2000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="Hostname to analyze (e.g. linkedin.com)")
    parser.add_argument("--limit", type=int, default=5, help="Max failed scrapes to analyze (default: 5)")
    parser.add_argument("--write", action="store_true",
                        help="Apply safe suggestions to the profile. Default is propose-only.")
    parser.add_argument("--mcp-url", default=os.environ.get("CC_MCP_URL", DEFAULT_MCP_URL),
                        help="MCP server URL (default: %(default)s)")
    return parser.parse_args()


def _unwrap(result: Any) -> Any:
    """fastmcp Client returns a CallToolResult; pull out the JSON payload."""
    if hasattr(result, "data") and result.data is not None:
        return result.data
    # Fallback: structured content list
    if hasattr(result, "content"):
        for part in result.content:
            text = getattr(part, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return result


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip("www.")


async def _fetch_failed_scrapes(client: Client, domain: str, limit: int) -> list[dict]:
    """Pull recent failed scrapes, client-side filter by hostname."""
    # Over-fetch so we have enough after host filtering.
    per_page = max(limit * 4, 20)
    result = await client.call_tool(
        "get_scrapes",
        {"status": "failed", "sort": "-id", "per_page": per_page},
    )
    payload = _unwrap(result)
    if isinstance(payload, str):
        payload = json.loads(payload)
    body = (payload.get("data") or {}) if isinstance(payload, dict) else {}
    scrapes = body.get("data") if isinstance(body, dict) else None
    if not scrapes:
        # Alternate shape: top-level data is the list.
        scrapes = payload.get("data") if isinstance(payload, dict) else []
    matched = []
    for s in scrapes or []:
        url = (s.get("attributes") or {}).get("url", "")
        if domain.lower() in _host_of(url) or domain.lower() in url.lower():
            matched.append(s)
            if len(matched) >= limit:
                break
    return matched


async def _list_screenshots(client: Client, scrape_id: int) -> list[str]:
    result = await client.call_tool("list_scrape_screenshots", {"scrape_id": scrape_id})
    payload = _unwrap(result)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []
    body = payload.get("data", payload)
    if isinstance(body, dict):
        items = body.get("data") or body.get("screenshots") or []
    else:
        items = body
    filenames = []
    for item in items or []:
        if isinstance(item, dict):
            fn = item.get("filename") or item.get("name")
            if fn:
                filenames.append(fn)
        elif isinstance(item, str):
            filenames.append(item)
    return filenames


async def _fetch_screenshot_bytes(client: Client, scrape_id: int, filename: str) -> bytes:
    result = await client.call_tool(
        "fetch_scrape_screenshot", {"scrape_id": scrape_id, "filename": filename},
    )
    payload = _unwrap(result)
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(f"fetch_scrape_screenshot failed: {payload}")
    return base64.b64decode(payload["data_base64"])


async def _get_profile(client: Client, hostname: str) -> dict | None:
    result = await client.call_tool("get_scrape_profile", {"hostname": hostname})
    payload = _unwrap(result)
    if isinstance(payload, str):
        payload = json.loads(payload)
    body = (payload or {}).get("data") if isinstance(payload, dict) else None
    profiles = body.get("data") if isinstance(body, dict) else None
    if not profiles:
        return None
    p = profiles[0] if isinstance(profiles, list) else profiles
    return {
        "id": int(p["id"]),
        "css_selectors": (p.get("attributes") or {}).get("css-selectors") or {},
    }


async def _update_profile(client: Client, profile_id: int, css_selectors: dict) -> None:
    await client.call_tool(
        "update_scrape_profile",
        {"profile_id": profile_id, "css_selectors": css_selectors},
    )


def _merge_analyses_into_css_selectors(
    existing: dict, analyses: list[tuple[int, ScreenshotAnalysis]],
) -> dict:
    """Apply auto-write policy. Returns a new css_selectors dict."""
    updated = dict(existing)
    notes = list(updated.get("analyzer_notes") or [])
    for scrape_id, a in analyses:
        notes.append({
            "scrape_id": scrape_id,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "failure_mode": a.failure_mode,
            "summary": a.summary,
            "confidence": a.confidence,
        })
    updated["analyzer_notes"] = notes[-50:]  # cap history at last 50

    existing_hints = (updated.get("interaction_hints") or "").strip()
    existing_bullets = {ln.lstrip("- ").strip() for ln in existing_hints.splitlines() if ln.strip()}
    new_bullets: list[str] = []
    for _, a in analyses:
        h = (a.suggested_interaction_hint or "").strip()
        if not h or h in existing_bullets:
            continue
        if a.confidence == "low":
            continue
        existing_bullets.add(h)
        new_bullets.append(h)
    if new_bullets:
        parts = [existing_hints] if existing_hints else []
        for b in new_bullets:
            parts.append(f"- {b}")
        merged = "\n".join(parts).strip()
        if len(merged) <= INTERACTION_HINTS_CAP:
            updated["interaction_hints"] = merged
        else:
            logger.warning(
                "interaction_hints would exceed %d chars; skipping %d new bullet(s)",
                INTERACTION_HINTS_CAP, len(new_bullets),
            )
    return updated


def _print_report(
    domain: str,
    analyses: list[tuple[int, ScreenshotAnalysis]],
    applied: bool,
) -> None:
    print()
    print(f"=== Screenshot analysis report for {domain} ===")
    print(f"Scrapes analyzed: {len(analyses)}")
    print(f"Write mode: {'APPLIED' if applied else 'propose-only (--write to apply)'}")
    print()
    for scrape_id, a in analyses:
        print(f"--- scrape {scrape_id} [{a.failure_mode}, confidence={a.confidence}] ---")
        print(f"  summary: {a.summary}")
        if a.suggested_interaction_hint:
            print(f"  interaction_hint (safe write): {a.suggested_interaction_hint}")
        if a.suggested_ready_selector:
            print(f"  PROPOSED ready_selector: {a.suggested_ready_selector}")
        if a.suggested_obstacle_click_selector:
            print(f"  PROPOSED obstacle_click_selector: {a.suggested_obstacle_click_selector}")
        print()

    load_bearing = [
        (sid, a) for sid, a in analyses
        if a.suggested_ready_selector or a.suggested_obstacle_click_selector
    ]
    if load_bearing:
        print("=== Proposed load-bearing selector edits (MANUAL REVIEW) ===")
        for sid, a in load_bearing:
            if a.suggested_ready_selector:
                print(f"  scrape {sid}: ready_selector = {a.suggested_ready_selector!r}")
            if a.suggested_obstacle_click_selector:
                print(f"  scrape {sid}: obstacle_click_selector = {a.suggested_obstacle_click_selector!r}")
        print()


async def main_async() -> int:
    args = _parse_args()
    token = os.environ.get("CC_API_TOKEN")
    if not token:
        logger.error("CC_API_TOKEN is required (jh_* staff key)")
        return 1

    # fastmcp Client supports bearer auth via the `auth` kwarg as a string token.
    async with Client(args.mcp_url, auth=token) as client:
        logger.info("Connected to MCP at %s", args.mcp_url)

        scrapes = await _fetch_failed_scrapes(client, args.domain, args.limit)
        if not scrapes:
            logger.info("No failed scrapes found for %s", args.domain)
            return 0
        logger.info("Found %d failed scrape(s) for %s", len(scrapes), args.domain)

        analyses: list[tuple[int, ScreenshotAnalysis]] = []
        for s in scrapes:
            scrape_id = int(s["id"])
            attrs = s.get("attributes") or {}
            url = attrs.get("url", "")
            note = attrs.get("note")
            try:
                filenames = await _list_screenshots(client, scrape_id)
            except Exception as exc:
                logger.warning("scrape %s: list screenshots failed: %s", scrape_id, exc)
                continue
            if not filenames:
                logger.info("scrape %s: no screenshots", scrape_id)
                continue
            try:
                png = await _fetch_screenshot_bytes(client, scrape_id, filenames[-1])
            except Exception as exc:
                logger.warning("scrape %s: fetch screenshot failed: %s", scrape_id, exc)
                continue
            try:
                analysis = await analyze_screenshot(
                    png_bytes=png, url=url, failure_note=note,
                )
            except Exception as exc:
                logger.warning("scrape %s: agent failed: %s", scrape_id, exc)
                continue
            analyses.append((scrape_id, analysis))
            logger.info(
                "scrape %s analyzed: %s (confidence=%s)",
                scrape_id, analysis.failure_mode, analysis.confidence,
            )

        if not analyses:
            logger.info("No analyses produced.")
            return 0

        applied = False
        if args.write:
            profile = await _get_profile(client, args.domain)
            if not profile:
                logger.warning("No profile found for %s; skipping write.", args.domain)
            else:
                new_css = _merge_analyses_into_css_selectors(
                    profile["css_selectors"], analyses,
                )
                if new_css != profile["css_selectors"]:
                    await _update_profile(client, profile["id"], new_css)
                    applied = True
                    logger.info("Profile %s updated.", profile["id"])
                else:
                    logger.info("No auto-writeable changes for profile %s.", profile["id"])

        _print_report(args.domain, analyses, applied)
    return 0


def run():
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    run()
