#!/usr/bin/env python3
"""Analyze screenshots of failed scrapes for a domain; suggest ScrapeProfile
improvements and optionally write safe fields back.

Connects to the Career Caddy public MCP server (careercaddy.online/mcp)
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
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastmcp import Client

from src.agents.html_fetchers import BrowserFetcher, HtmlFetcher, StoredHtmlFetcher
from src.agents.screenshot_analyzer import ScreenshotAnalysis, analyze_screenshot
from src.agents.selector_grounder import GroundedSelectors, ground_selectors

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("analyze_screenshots")

DEFAULT_MCP_URL = "https://careercaddy.online/mcp"
INTERACTION_HINTS_CAP = 2000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--domain",
        help="Hostname to analyze (e.g. linkedin.com). Required unless --scrape-id is given.",
    )
    parser.add_argument(
        "--scrape-id",
        type=int,
        action="append",
        default=None,
        help="Analyze a specific scrape id (repeatable). Bypasses status/domain filters.",
    )
    parser.add_argument("--limit", type=int, default=5, help="Max scrapes to analyze (default: 5)")
    parser.add_argument(
        "--status",
        action="append",
        default=None,
        help="Scrape status(es) to include. Repeatable. Default: failed, error.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply safe suggestions to the profile. Default is propose-only.",
    )
    parser.add_argument(
        "--revisit",
        action="store_true",
        help="On grounding miss, re-fetch the page via a real browser "
        "(Camoufox) so JS-rendered DOM is grounded. Reuses any saved "
        "login session for the domain. Slower; off by default.",
    )
    parser.add_argument(
        "--revisit-headed",
        action="store_true",
        help="Run --revisit browser with a visible window (useful for debugging).",
    )
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("CC_MCP_URL", DEFAULT_MCP_URL),
        help="MCP server URL (default: %(default)s)",
    )
    args = parser.parse_args()
    if not args.scrape_id and not args.domain:
        parser.error("--domain is required unless --scrape-id is given")
    return args


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
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _extract_scrape_list(payload: Any) -> list[dict]:
    """Handle both {data: {data: [...]}} and {data: [...]} shapes."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return []
    body = payload.get("data")
    if isinstance(body, dict):
        inner = body.get("data")
        return inner if isinstance(inner, list) else []
    return body if isinstance(body, list) else []


async def _fetch_scrape_by_id(client: Client, scrape_id: str) -> dict | None:
    result = await client.call_tool("get_scrapes", {"id": scrape_id})
    payload = _unwrap(result)
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    body = payload.get("data", payload)
    # get_scrapes with id returns a single resource object, not a list.
    if isinstance(body, dict) and body.get("id") and body.get("attributes"):
        return body
    if isinstance(body, dict):
        inner = body.get("data")
        if isinstance(inner, dict):
            return inner
    return None


async def _fetch_scrapes_for_domain(
    client: Client,
    domain: str,
    limit: int,
    statuses: list[str],
) -> list[dict]:
    """Pull recent scrapes across the given statuses, client-side filter by hostname."""
    per_page = max(limit * 4, 20)
    seen_ids: set[str] = set()
    matched: list[dict] = []
    d = domain.lower()
    for status in statuses:
        result = await client.call_tool(
            "get_scrapes",
            {"status": status, "sort": "-id", "per_page": per_page},
        )
        for s in _extract_scrape_list(_unwrap(result)):
            sid = str(s.get("id") or "")
            if not sid or sid in seen_ids:
                continue
            url = (s.get("attributes") or {}).get("url", "")
            if d in _host_of(url) or d in url.lower():
                seen_ids.add(sid)
                matched.append(s)
    matched.sort(key=lambda s: str(s.get("id") or ""), reverse=True)
    return matched[:limit]


async def _list_screenshots(client: Client, scrape_id: str) -> list[str]:
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


async def _fetch_screenshot_bytes(client: Client, scrape_id: str, filename: str) -> bytes:
    result = await client.call_tool(
        "fetch_scrape_screenshot",
        {"scrape_id": scrape_id, "filename": filename},
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
        "id": str(p["id"]),
        "css_selectors": (p.get("attributes") or {}).get("css-selectors") or {},
    }


async def _update_profile(client: Client, profile_id: str, css_selectors: dict) -> None:
    await client.call_tool(
        "update_scrape_profile",
        {"profile_id": profile_id, "css_selectors": css_selectors},
    )


AnalysisRow = tuple[int, ScreenshotAnalysis, GroundedSelectors | None]

# Failure modes where there's nothing to ground — no obstacle to click, no
# ready marker the LLM could point at without the real job body being visible.
_NO_GROUNDING = {"expired_listing", "empty_content", "unknown", "rate_limit", "geo_block"}


def _has_selectors(g: GroundedSelectors | None) -> bool:
    return bool(g and (g.obstacle_click_selector or g.ready_selector))


async def _ground_with_escalation(
    *,
    url: str,
    analysis: ScreenshotAnalysis,
    primary: HtmlFetcher,
    fallback: HtmlFetcher | None,
) -> tuple[GroundedSelectors | None, str]:
    """Try the primary fetcher; if it produces no grounded selectors and a
    fallback is configured, escalate. Returns (grounded, source_label)."""
    grounded: GroundedSelectors | None = None
    html = await primary.fetch(url)
    if html:
        grounded = await ground_selectors(
            html=html,
            failure_mode=analysis.failure_mode,
            summary=analysis.summary,
            interaction_hint=analysis.suggested_interaction_hint,
        )
    if _has_selectors(grounded) or fallback is None:
        return grounded, "stored" if html else "none"

    logger.info("stored-HTML grounding miss for %s — escalating to browser revisit", url)
    html = await fallback.fetch(url)
    if not html:
        return grounded, "browser (fetch failed)"
    grounded = await ground_selectors(
        html=html,
        failure_mode=analysis.failure_mode,
        summary=analysis.summary,
        interaction_hint=analysis.suggested_interaction_hint,
    )
    return grounded, "browser"


def _merge_analyses_into_css_selectors(
    existing: dict,
    rows: list[AnalysisRow],
) -> dict:
    """Apply auto-write policy. Returns a new css_selectors dict."""
    updated = dict(existing)
    notes = list(updated.get("analyzer_notes") or [])
    for scrape_id, a, _g in rows:
        notes.append(
            {
                "scrape_id": scrape_id,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "failure_mode": a.failure_mode,
                "summary": a.summary,
                "confidence": a.confidence,
            }
        )
    updated["analyzer_notes"] = notes[-50:]

    existing_hints = (updated.get("interaction_hints") or "").strip()
    existing_bullets = {ln.lstrip("- ").strip() for ln in existing_hints.splitlines() if ln.strip()}
    new_bullets: list[str] = []
    for _, a, _g in rows:
        h = (a.suggested_interaction_hint or "").strip()
        if not h or h in existing_bullets:
            continue
        if a.confidence == "low":
            continue
        if a.failure_mode == "expired_listing":
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
                INTERACTION_HINTS_CAP,
                len(new_bullets),
            )
    return updated


def _print_report(target: str, rows: list[AnalysisRow], applied: bool) -> None:
    print()
    print(f"=== Screenshot analysis report for {target} ===")
    print(f"Scrapes analyzed: {len(rows)}")
    print(f"Write mode: {'APPLIED' if applied else 'propose-only (--write to apply)'}")
    print()
    for scrape_id, a, g in rows:
        print(f"--- scrape {scrape_id} [{a.failure_mode}, confidence={a.confidence}] ---")
        print(f"  summary: {a.summary}")
        if a.suggested_interaction_hint:
            print(f"  interaction_hint (safe write): {a.suggested_interaction_hint}")
        if g is None:
            print("  selector grounding: skipped (no HTML or non-groundable failure)")
        else:
            if g.obstacle_click_selector or g.ready_selector:
                print(f"  grounded selectors (HTML-validated, confidence={g.confidence}):")
                if g.obstacle_click_selector:
                    print(f"    obstacle_click_selector: {g.obstacle_click_selector}")
                if g.ready_selector:
                    print(f"    ready_selector: {g.ready_selector}")
                if g.reasoning:
                    print(f"    reasoning: {g.reasoning}")
            else:
                print(f"  selector grounding: no HTML match (confidence={g.confidence})")
        print()

    expired = [(sid, a) for sid, a, _ in rows if a.failure_mode == "expired_listing"]
    if expired:
        print("=== Expired listings (dead upstream — close the application) ===")
        for sid, a in expired:
            print(f"  scrape {sid}: {a.summary}")
        print()

    load_bearing = [
        (sid, g) for sid, _a, g in rows if g and (g.obstacle_click_selector or g.ready_selector)
    ]
    if load_bearing:
        print("=== Proposed load-bearing selector edits (MANUAL REVIEW) ===")
        for sid, g in load_bearing:
            if g.obstacle_click_selector:
                print(f"  scrape {sid}: obstacle_click_selector = {g.obstacle_click_selector!r}")
            if g.ready_selector:
                print(f"  scrape {sid}: ready_selector = {g.ready_selector!r}")
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

        if args.scrape_id:
            scrapes = []
            for sid in args.scrape_id:
                s = await _fetch_scrape_by_id(client, sid)
                if s is None:
                    logger.warning("scrape %s: not found", sid)
                    continue
                scrapes.append(s)
            target = f"ids={args.scrape_id}"
        else:
            statuses = args.status or ["failed", "error"]
            scrapes = await _fetch_scrapes_for_domain(
                client,
                args.domain,
                args.limit,
                statuses,
            )
            target = f"{args.domain} (statuses: {','.join(statuses)})"
        if not scrapes:
            logger.info("No matching scrapes for %s", target)
            return 0
        logger.info("Found %d scrape(s) for %s", len(scrapes), target)

        fallback_fetcher: HtmlFetcher | None = None
        if args.revisit:
            fallback_fetcher = BrowserFetcher(headless=not args.revisit_headed)
            logger.info("--revisit enabled: browser fallback on grounding miss")

        rows: list[AnalysisRow] = []
        for s in scrapes:
            scrape_id = str(s["id"])
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
                    png_bytes=png,
                    url=url,
                    failure_note=note,
                )
            except Exception as exc:
                logger.warning("scrape %s: vision pass failed: %s", scrape_id, exc)
                continue
            logger.info(
                "scrape %s vision: %s (confidence=%s)",
                scrape_id,
                analysis.failure_mode,
                analysis.confidence,
            )

            grounded: GroundedSelectors | None = None
            if analysis.failure_mode not in _NO_GROUNDING and url:
                try:
                    grounded, source = await _ground_with_escalation(
                        url=url,
                        analysis=analysis,
                        primary=StoredHtmlFetcher(client, scrape_id),
                        fallback=fallback_fetcher,
                    )
                    if grounded:
                        logger.info(
                            "scrape %s grounding (%s): obstacle=%r ready=%r",
                            scrape_id,
                            source,
                            grounded.obstacle_click_selector,
                            grounded.ready_selector,
                        )
                    else:
                        logger.info("scrape %s grounding: no HTML (%s)", scrape_id, source)
                except Exception as exc:
                    logger.warning("scrape %s: grounding failed: %s", scrape_id, exc)

            rows.append((scrape_id, analysis, grounded))

        if not rows:
            logger.info("No analyses produced.")
            return 0

        applied = False
        if args.write:
            write_domain = args.domain
            if not write_domain and scrapes:
                # Derive from the first scrape's URL when --scrape-id was used.
                write_domain = _host_of((scrapes[0].get("attributes") or {}).get("url", ""))
            profile = await _get_profile(client, write_domain) if write_domain else None
            if not profile:
                logger.warning("No profile found for %s; skipping write.", write_domain or "—")
            else:
                new_css = _merge_analyses_into_css_selectors(
                    profile["css_selectors"],
                    rows,
                )
                if new_css != profile["css_selectors"]:
                    await _update_profile(client, profile["id"], new_css)
                    applied = True
                    logger.info("Profile %s updated.", profile["id"])
                else:
                    logger.info("No auto-writeable changes for profile %s.", profile["id"])

        _print_report(args.domain or f"ids={args.scrape_id}", rows, applied)
    return 0


def run():
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    run()
