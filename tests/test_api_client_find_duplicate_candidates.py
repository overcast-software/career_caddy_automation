"""Unit tests for ``api_client.find_duplicate_candidates`` — the
operator-side, REST-only twin of the public-MCP duplicate-candidate
composite.

Contract under test:

1. ``link`` exact match  → confidence "high",  signal "link"
   (GET /api/v1/job-posts/?filter[link]=…).
2. ``company`` resolved → its posts listed (filter[company_id]) →
   local title compare:
     * normalized-equal title → "high",   signal "title_exact".
     * prefix/suffix overlap  → "medium", signal "title_similarity".
3. A short / low-overlap incoming title does NOT match (conservative
   guards — a missed near-dupe is safer than a false positive).
4. Title alone (no link, no company) → [] (too low-signal).
5. Fail-safe: any api failure → [] (caller then fails OPEN and POSTs).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from src.client.api_client import find_duplicate_candidates


def _row(post_id: int, title: str, company_name: str | None = None) -> dict:
    return {
        "id": str(post_id),
        "type": "job-post",
        "attributes": {"title": title, "company_name": company_name},
        "_frontend_url": f"/job-posts/{post_id}",
    }


def _make_api(*, link_rows=None, company_id=None, company_posts=None) -> MagicMock:
    """Route api.get by path/params to the right canned JSON:API envelope."""

    async def fake_get(path, params=None):
        params = params or {}
        if path == "/api/v1/companies/":
            companies = [{"id": str(company_id)}] if company_id is not None else []
            return json.dumps({"success": True, "data": {"data": companies}, "status_code": 200})
        if path == "/api/v1/job-posts/":
            if "filter[link]" in params:
                rows = link_rows or []
            elif "filter[company_id]" in params:
                rows = company_posts or []
            else:
                rows = []
            return json.dumps({"success": True, "data": {"data": rows}, "status_code": 200})
        return json.dumps({"success": True, "data": {"data": []}, "status_code": 200})

    api = MagicMock()
    api.get = AsyncMock(side_effect=fake_get)
    return api


class TestFindDuplicateCandidates:
    def test_exact_link_match_is_high(self):
        api = _make_api(link_rows=[_row(5, "Engineer", "Acme")])
        out = asyncio.run(
            find_duplicate_candidates(api, title="Engineer", link="https://x.test/jobs/5")
        )
        assert len(out) == 1
        assert out[0].id == 5
        assert out[0].confidence == "high"
        assert "link" in out[0].match_signals
        assert out[0].frontend_url == "/job-posts/5"

    def test_same_company_exact_title_is_high(self):
        api = _make_api(company_id=10, company_posts=[_row(7, "Senior Backend Engineer")])
        out = asyncio.run(
            find_duplicate_candidates(api, title="Senior Backend Engineer", company="Acme")
        )
        assert len(out) == 1
        assert out[0].id == 7
        assert out[0].confidence == "high"
        assert "title_exact" in out[0].match_signals

    def test_same_company_suffix_drift_is_medium(self):
        # The aggregator-relist case: same role, one title carries a
        # trailing suffix. Distinct fingerprints; title_similarity fires.
        api = _make_api(
            company_id=10,
            company_posts=[_row(8, "ID.me Authentication Engineer 75-100% FTE")],
        )
        out = asyncio.run(
            find_duplicate_candidates(api, title="ID.me Authentication Engineer", company="ID.me")
        )
        assert len(out) == 1
        assert out[0].id == 8
        assert out[0].confidence == "medium"
        assert "title_similarity" in out[0].match_signals

    def test_unrelated_title_no_match(self):
        api = _make_api(company_id=10, company_posts=[_row(9, "Completely Different Role")])
        out = asyncio.run(find_duplicate_candidates(api, title="Engineer", company="Acme"))
        assert out == []

    def test_short_incoming_title_guarded_out(self):
        # "Eng" is too short to anchor a prefix/suffix match against
        # "Engineering Manager" — guard must reject it.
        api = _make_api(company_id=10, company_posts=[_row(1, "Engineering Manager")])
        out = asyncio.run(find_duplicate_candidates(api, title="Eng", company="Acme"))
        assert out == []

    def test_low_overlap_ratio_guarded_out(self):
        # A small shared prefix swamped by a long suffix is a different
        # scope, not a near-dupe.
        api = _make_api(
            company_id=10,
            company_posts=[_row(2, "Authentication Engineer Lead Principal Staff Architect")],
        )
        out = asyncio.run(find_duplicate_candidates(api, title="Authentication", company="Acme"))
        assert out == []

    def test_title_only_returns_empty(self):
        api = _make_api()
        out = asyncio.run(find_duplicate_candidates(api, title="Engineer"))
        assert out == []
        # No link and no company → no lookups worth making.
        api.get.assert_not_awaited()

    def test_link_and_title_match_same_post_merges_signals(self):
        api = _make_api(
            link_rows=[_row(5, "Engineer")],
            company_id=10,
            company_posts=[_row(5, "Engineer")],
        )
        out = asyncio.run(
            find_duplicate_candidates(
                api, title="Engineer", company="Acme", link="https://x.test/5"
            )
        )
        assert len(out) == 1
        assert out[0].id == 5
        assert out[0].confidence == "high"
        assert set(out[0].match_signals) == {"link", "title_exact"}

    def test_unknown_company_yields_no_title_candidates(self):
        api = _make_api(company_id=None, company_posts=[_row(7, "Engineer")])
        out = asyncio.run(find_duplicate_candidates(api, title="Engineer", company="Ghost Inc"))
        assert out == []

    def test_api_failure_is_fail_safe_empty(self):
        api = MagicMock()
        api.get = AsyncMock(side_effect=RuntimeError("connection reset"))
        out = asyncio.run(
            find_duplicate_candidates(
                api, title="Engineer", company="Acme", link="https://x.test/5"
            )
        )
        assert out == []
