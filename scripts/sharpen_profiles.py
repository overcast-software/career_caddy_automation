#!/usr/bin/env python3
"""Offline ScrapeProfile sharpening.

Analyzes recent scrapes for a hostname and proposes ScrapeProfile
refinements — currently focused on `url_rewrites`, the field that
would have saved scrape 175 (indeed `?vjk=X` homepage → `/viewjob?jk=X`).

The idea: the live pipeline's EvaluateExtraction + ValidateExtraction
catch *some* bad data at ingest time. This script catches the rest
after the fact by grouping recent scrapes by URL shape and looking for
the signatures of a "soft" false positive — short job_content on
tracker-style URLs where a sibling URL-shape on the same host
consistently produces a fatter body.

First cut is dry-run only. Once we've validated the suggestions
against a few hosts we'll add a --write path that PATCHes the profile
via the MCP `update_scrape_profile` tool.

Usage:
    uv run python scripts/sharpen_profiles.py --hostname indeed.com --limit 40
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from statistics import median
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastmcp import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("sharpen_profiles")

DEFAULT_MCP_URL = "https://mcp.careercaddy.online/mcp"

# Classification thresholds. Tuned against scrapes 172/174/175 — not
# yet validated on a broader corpus; expect these to move once the
# script runs against more hosts.
THIN_CONTENT_WORDS = 120  # below this = likely not a full posting
SUSPICIOUS_LIST_SIGNALS = ("easily apply", "new\n", "full-time")
LIST_SIGNAL_HITS_MIN = 3  # that many repeated listing-ish phrases = list page


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hostname", required=True, help="e.g. indeed.com")
    p.add_argument("--limit", type=int, default=40, help="recent scrapes to pull")
    p.add_argument(
        "--mcp-url",
        default=os.environ.get("CC_MCP_URL", DEFAULT_MCP_URL),
        help="MCP endpoint (default: %(default)s)",
    )
    return p.parse_args()


def _unwrap(result: Any) -> Any:
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


def _scrape_list(payload: Any) -> list[dict]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return []
    body = payload.get("data")
    if isinstance(body, dict):
        inner = body.get("data")
        return inner if isinstance(inner, list) else []
    return body if isinstance(body, list) else []


def _host_of(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _url_shape(url: str) -> str:
    """Canonical shape for grouping: path + sorted query-param keys.

    Distinguishes `indeed.com/viewjob?jk=X` (real job) from
    `indeed.com/?vjk=X` (homepage) — the diagnostic signal we care
    about. Values are dropped; only the *structure* of the URL matters
    for classifying good vs bad shapes.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    keys = sorted(parse_qs(parsed.query).keys())
    return f"{parsed.path or '/'}?{','.join(keys)}" if keys else (parsed.path or "/")


def _word_count(text: str | None) -> int:
    return len((text or "").split())


def _list_signal_hits(text: str | None) -> int:
    lowered = (text or "").lower()
    return sum(lowered.count(sig) for sig in SUSPICIOUS_LIST_SIGNALS)


def _classify(attrs: dict) -> str:
    """Bucket a scrape into clean-success / suspicious-success / failed."""
    status = attrs.get("status")
    if status not in ("completed", "success"):
        return "failed"
    content = attrs.get("job_content") or ""
    if _word_count(content) < THIN_CONTENT_WORDS:
        return "suspicious-thin"
    if _list_signal_hits(content) >= LIST_SIGNAL_HITS_MIN:
        return "suspicious-list"
    return "clean"


async def _fetch_recent(client: Client, hostname: str, limit: int) -> list[dict]:
    """Pull ~limit most-recent scrapes, then filter to hostname client-side.

    MCP's get_scrapes doesn't expose a hostname filter directly; we
    over-fetch and drop non-matches. limit*4 is a crude heuristic —
    raise it if a host is sparse.
    """
    per_page = max(limit * 4, 40)
    result = await client.call_tool("get_scrapes", {"sort": "-id", "per_page": per_page})
    rows = _scrape_list(_unwrap(result))
    matched = []
    for row in rows:
        attrs = (row or {}).get("attributes") or {}
        if _host_of(attrs.get("url", "")) == hostname:
            matched.append(row)
        if len(matched) >= limit:
            break
    return matched


def _propose_rewrites(grouped: dict[str, list[dict]]) -> list[dict]:
    """Suggest url_rewrites rules based on shape-level outcome stats.

    Simple first-cut heuristic: if shape A is consistently suspicious
    and contains a param whose value also appears in a clean shape B's
    path, propose rewriting A → B-template. Example:

        "/?advn,vjk"           (suspicious-thin, suspicious-list)
        "/viewjob?jk"          (clean)
        vjk value overlaps with jk → propose rewrite.

    Known-host seeds live here for now; the heuristic is opt-in data
    and the seeds carry the load until we've validated more cases.
    """
    suggestions: list[dict] = []

    # Indeed seed — motivated by scrape 175. Safe because the rewrite
    # lands on the same host and preserves the job key.
    indeed_has_suspicious_vjk = any(
        "vjk" in shape and bucket.startswith("suspicious")
        for shape, rows in grouped.items()
        for bucket in [_majority_bucket(rows)]
    )
    if indeed_has_suspicious_vjk:
        suggestions.append(
            {
                "match": r"^https?://(?:www\.)?indeed\.com/\?[^#]*\bvjk=([A-Za-z0-9]+)",
                "rewrite": r"https://www.indeed.com/viewjob?jk=\1",
                "reason": "Indeed tracker `?vjk=X` lands on homepage; `/viewjob?jk=X` is the real job page.",
            }
        )

    return suggestions


def _majority_bucket(rows: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[_classify((row or {}).get("attributes") or {})] += 1
    return max(counts, key=lambda k: counts[k]) if counts else "unknown"


def _print_report(hostname: str, grouped: dict[str, list[dict]], suggestions: list[dict]) -> None:
    print(f"\nHost: {hostname}")
    print(f"URL shapes seen ({sum(len(v) for v in grouped.values())} scrapes):\n")
    print(f"  {'shape':<45} {'n':>4} {'bucket':<22} {'median_words':>12}")
    for shape, rows in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        bucket = _majority_bucket(rows)
        word_counts = [_word_count((r.get("attributes") or {}).get("job_content")) for r in rows]
        mw = int(median(word_counts)) if word_counts else 0
        print(f"  {shape[:45]:<45} {len(rows):>4} {bucket:<22} {mw:>12}")

    if suggestions:
        print(f"\nProposed url_rewrites for {hostname}:\n")
        for s in suggestions:
            print(f"  match  : {s['match']}")
            print(f"  rewrite: {s['rewrite']}")
            print(f"  why    : {s['reason']}\n")
        print("To apply, wire these into the ScrapeProfile.url_rewrites field")
        print("(dry-run only for now — --write support coming once validated).")
    else:
        print("\nNo url_rewrite suggestions.")


async def _run(hostname: str, limit: int, mcp_url: str) -> int:
    token = os.environ.get("CC_API_TOKEN") or os.environ.get("CAREER_CADDY_API_TOKEN")
    if not token:
        logger.error("CC_API_TOKEN (or CAREER_CADDY_API_TOKEN) must be set")
        return 2
    async with Client(mcp_url, auth=token) as client:
        rows = await _fetch_recent(client, hostname, limit)
    if not rows:
        logger.warning("no recent scrapes found for %s", hostname)
        return 1

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        attrs = (row or {}).get("attributes") or {}
        grouped[_url_shape(attrs.get("url", ""))].append(row)

    suggestions = _propose_rewrites(grouped)
    _print_report(hostname, grouped, suggestions)
    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args.hostname, args.limit, args.mcp_url))


if __name__ == "__main__":
    sys.exit(main())
