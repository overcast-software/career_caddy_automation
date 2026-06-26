"""AUTO-29 — known-good free-tier auto-enrichment in the notmuch triage path.

Doug's Phase 3 ("morning descriptions, only when free"): after caddy-inbox
stage 5 creates a JobPost, if the post's host is *known-good* (the api can
extract it with its $0 deterministic Tier-0 CSS pass, no LLM) we queue a
``hold`` scrape with ``auto_score=False`` so the runner fills the description
without ever spending scrape *or* scoring tokens. The behavior regressed when
AUTO-26 deleted ``src/pollers/email_catchall.py``; this re-ports it inline.

Contract pinned here:

* Opt-in: only fires when ``CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD`` is set
  (default OFF — flag off means no readiness lookup and no scrape at all).
* Free guarantee: the hold scrape carries ``auto_score=False`` so scoring
  never runs.
* Known-good only: a non-known-good host (and a readiness miss) queues
  nothing.
* Dedupe-aware: an existing scrape for the post short-circuits the create.
* Fail-safe: any error — including a readiness lookup that raises — leaves
  JobPost creation untouched and never propagates.

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import scripts.inbox_triage as it

FLAG = "CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD"
ATTENDED_FLAG = "CADDY_FORWARD_ATTENDED_KNOWN_GOOD"
# Real NanoID-shaped id — numeric-string ids would mask an int()-cast regression.
POST_ID = "V30p4hHABQ"


@dataclass
class _Link:
    url: str
    title: str = "Staff Engineer"
    company: str | None = None
    description: str | None = None


def _profile(*, is_known_good: bool, tier: str | None) -> dict:
    return {
        "id": "1",
        "type": "scrape-profile",
        "attributes": {
            "is_known_good": is_known_good,
            "readiness": {"known_good": is_known_good, "tier": tier, "reasons": []},
        },
    }


def _make_api(
    *,
    profile: dict | None = None,
    scrape_rows: list[dict] | None = None,
    jobpost_status: int = 201,
) -> MagicMock:
    """Route a mocked ApiClient by endpoint.

    GET  /scrape-profiles/ → readiness (``profile`` or an empty list = miss)
    GET  /scrapes/         → existing scrapes (``scrape_rows`` or empty)
    POST /job-posts/       → fresh JobPost create envelope (id = POST_ID)
    POST /scrapes/         → scrape create success envelope
    """
    api = MagicMock()

    async def _get(path: str, params: dict | None = None) -> str:
        if path == "/api/v1/scrape-profiles/":
            rows = [profile] if profile is not None else []
            return json.dumps({"success": True, "data": {"data": rows}, "status_code": 200})
        if path == "/api/v1/scrapes/":
            return json.dumps({"success": True, "data": {"data": scrape_rows or []}})
        raise AssertionError(f"unexpected GET {path}")

    async def _post(path: str, payload: dict) -> str:
        if path == "/api/v1/job-posts/":
            return json.dumps(
                {
                    "success": True,
                    "status_code": jobpost_status,
                    "data": {"data": {"id": POST_ID, "attributes": {"canonical_link": None}}},
                }
            )
        if path == "/api/v1/scrapes/":
            return json.dumps({"success": True, "data": {"data": {"id": "S1", "type": "scrape"}}})
        raise AssertionError(f"unexpected POST {path}")

    api.get = AsyncMock(side_effect=_get)
    api.post = AsyncMock(side_effect=_post)
    return api


def _scrape_post_attrs(api: MagicMock) -> list[dict]:
    return [
        call.args[1]["data"]["attributes"]
        for call in api.post.call_args_list
        if call.args[0] == "/api/v1/scrapes/"
    ]


def _profile_lookups(api: MagicMock) -> list[dict]:
    return [call for call in api.get.call_args_list if call.args[0] == "/api/v1/scrape-profiles/"]


# ---------------------------------------------------------------------------
# _enrich_known_good — the helper in isolation
# ---------------------------------------------------------------------------


class TestEnrichKnownGood:
    def test_known_good_queues_hold_scrape_with_auto_score_false(self):
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert outcome == "created"
        attrs = _scrape_post_attrs(api)
        assert len(attrs) == 1
        assert attrs[0]["status"] == "hold"
        # The $0 guarantee: scoring must never run on an auto-enrichment scrape.
        assert attrs[0]["auto_score"] is False

    def test_effective_tier_zero_triggers_even_when_not_known_good(self):
        # A host pinned to deterministic Tier-0 is still free even if it hasn't
        # cleared the success-rate threshold for is_known_good.
        api = _make_api(profile=_profile(is_known_good=False, tier="0"))
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert outcome == "created"
        assert _scrape_post_attrs(api)[0]["auto_score"] is False

    def test_non_known_good_host_queues_nothing(self):
        api = _make_api(profile=_profile(is_known_good=False, tier="emerging"))
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert outcome == "skip"
        assert _scrape_post_attrs(api) == []

    def test_readiness_miss_queues_nothing(self):
        api = _make_api(profile=None)  # no ScrapeProfile for the host
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://ghost.example/j/1"))
        assert outcome == "skip"
        assert _scrape_post_attrs(api) == []

    def test_existing_scrape_dedupes(self):
        api = _make_api(
            profile=_profile(is_known_good=True, tier="verified"),
            scrape_rows=[{"id": "S0", "type": "scrape"}],
        )
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert outcome == "exists"
        assert _scrape_post_attrs(api) == []

    def test_www_stripped_from_hostname(self):
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        asyncio.run(it._enrich_known_good(api, POST_ID, "https://www.acme.com/jobs/1"))
        lookups = _profile_lookups(api)
        assert lookups[0].kwargs["params"] == {"filter[hostname]": "acme.com"}

    def test_readiness_raising_is_fail_safe(self, monkeypatch):
        # fetch_profile_readiness is fail-safe by contract, but the helper must
        # also swallow a hard raise so JobPost creation is never endangered.
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        monkeypatch.setattr(
            it, "fetch_profile_readiness", AsyncMock(side_effect=RuntimeError("boom"))
        )
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert outcome is None
        assert _scrape_post_attrs(api) == []


# ---------------------------------------------------------------------------
# Attended-routing gate (CC-97): the hold must NOT be marked attended unless
# CADDY_FORWARD_ATTENDED_KNOWN_GOOD is explicitly enabled. Default OFF keeps
# email scrapes on the unattended queue so a normal runner actually processes
# them — never stranded on the attended partition (the CC-96 brownout repro).
# ---------------------------------------------------------------------------


class TestAttendedKnownGoodGate:
    def test_flag_off_omits_attended(self, monkeypatch):
        # Default (flag unset): the hold goes to the unattended queue, so
        # create_scrape omits the `attended` attribute entirely.
        monkeypatch.delenv(ATTENDED_FLAG, raising=False)
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert outcome == "created"
        attrs = _scrape_post_attrs(api)
        assert len(attrs) == 1
        assert "attended" not in attrs[0]

    def test_flag_on_marks_attended_true(self, monkeypatch):
        monkeypatch.setenv(ATTENDED_FLAG, "1")
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        outcome = asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert outcome == "created"
        attrs = _scrape_post_attrs(api)
        assert attrs[0]["attended"] is True
        # Attended routing must not change the free guarantee.
        assert attrs[0]["auto_score"] is False

    def test_flag_falsey_value_omits_attended(self, monkeypatch):
        # A non-truthy value is treated as OFF, same as unset.
        monkeypatch.setenv(ATTENDED_FLAG, "0")
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        asyncio.run(it._enrich_known_good(api, POST_ID, "https://acme.com/jobs/1"))
        assert "attended" not in _scrape_post_attrs(api)[0]


# ---------------------------------------------------------------------------
# _create_posts_from_urls — flag gating + fail-safe at the stage-5 call site
# ---------------------------------------------------------------------------


class TestCreatePostsFromUrlsEnrichment:
    def test_flag_on_known_good_queues_scrape_and_creates_post(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        result = asyncio.run(
            it._create_posts_from_urls(api, [_Link(url="https://acme.com/jobs/1")])
        )
        assert result["created"] == ["https://acme.com/jobs/1"]
        assert result["scrapes_queued"] == 1
        attrs = _scrape_post_attrs(api)
        assert attrs[0]["status"] == "hold"
        assert attrs[0]["auto_score"] is False

    def test_flag_off_skips_readiness_and_scrape(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        result = asyncio.run(
            it._create_posts_from_urls(api, [_Link(url="https://acme.com/jobs/1")])
        )
        # JobPost still created — enrichment is purely additive.
        assert result["created"] == ["https://acme.com/jobs/1"]
        assert result["scrapes_queued"] == 0
        # No readiness lookup, no scrape POST when the flag is off.
        assert _profile_lookups(api) == []
        assert _scrape_post_attrs(api) == []

    def test_enrichment_failure_never_breaks_jobpost_create(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        api = _make_api(profile=_profile(is_known_good=True, tier="verified"))
        monkeypatch.setattr(
            it, "fetch_profile_readiness", AsyncMock(side_effect=RuntimeError("boom"))
        )
        result = asyncio.run(
            it._create_posts_from_urls(api, [_Link(url="https://acme.com/jobs/1")])
        )
        # The post is created; the enrichment blow-up is swallowed.
        assert result["created"] == ["https://acme.com/jobs/1"]
        assert result["scrapes_queued"] == 0
        assert _scrape_post_attrs(api) == []
