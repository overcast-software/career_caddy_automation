"""CC-133 — all-domain auto-scrape gate in the notmuch triage path.

Email-sourced JobPosts kept only the digest one-liner as description because
caddy-inbox never enqueued a scrape — the CADDY_AUTO_SCRAPE gate lived only in
the older ``process_tagged`` tag pipeline, not the live caddy-inbox daemon.
This wires the same gate into ``inbox_triage._create_posts_from_urls``: when
``CADDY_AUTO_SCRAPE`` is truthy, every NEWLY created (201) post gets a plain
``hold`` scrape carrying ``job_post_id`` so the idle runner claims it.

Contract pinned here:

* Opt-in: default OFF (no scrape POST when the flag is unset). Truthy contract
  matches ``process_tagged`` — 1 / true / yes / on.
* 201-only idempotency: a fresh create (201) is scraped; an api dedupe hit
  (200) queues nothing. There is deliberately NO ``get_scrapes`` pre-check —
  the api ignores ``filter[job_post_id]`` (claudex
  ``api-scrape-viewset-ignores-job-post-filter``), so the 201-only trigger IS
  the dedupe guard.
* All domains, zero heuristics: no ``requires_auth`` / known-good gating (v0).
* Fail-open: a scrape-create error never fails the JobPost triage.
* NanoID ids: real NanoID-shaped ids in fixtures — a numeric string would mask
  an ``int()``-cast regression (claudex ``never-int-cast-api-ids-nanoid``).

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import scripts.inbox_triage as it

FLAG = "CADDY_AUTO_SCRAPE"
KNOWN_GOOD_FLAG = "CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD"
# Real NanoID-shaped id — a numeric-string id would mask an int()-cast regression.
POST_ID = "V30p4hHABQ"
URL = "https://acme.com/jobs/staff-engineer"


@dataclass
class _Link:
    url: str
    title: str = "Staff Engineer"
    company: str | None = None
    description: str | None = None


def _make_api(
    *,
    jobpost_status: int = 201,
    profile: dict | None = None,
    scrape_create_success: bool = True,
    scrape_create_raises: bool = False,
) -> MagicMock:
    """Route a mocked ApiClient by endpoint.

    POST /job-posts/       → JobPost create envelope (status = jobpost_status)
    POST /scrapes/         → scrape create success/failure envelope
    GET  /scrape-profiles/ → readiness (``profile`` or empty list = miss)
    GET  /scrapes/         → empty existing-scrape set (known-good dedupe path)
    """
    api = MagicMock()

    async def _get(path: str, params: dict | None = None) -> str:
        if path == "/api/v1/scrape-profiles/":
            rows = [profile] if profile is not None else []
            return json.dumps({"success": True, "data": {"data": rows}, "status_code": 200})
        if path == "/api/v1/scrapes/":
            return json.dumps({"success": True, "data": {"data": []}})
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
            if scrape_create_raises:
                raise RuntimeError("boom")
            if scrape_create_success:
                return json.dumps(
                    {"success": True, "data": {"data": {"id": "S1", "type": "scrape"}}}
                )
            return json.dumps({"success": False, "error": "scrape create failed"})
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


def _scrape_post_rels(api: MagicMock) -> list[dict]:
    return [
        (call.args[1]["data"].get("relationships") or {})
        for call in api.post.call_args_list
        if call.args[0] == "/api/v1/scrapes/"
    ]


def _profile_lookups(api: MagicMock) -> list:
    return [call for call in api.get.call_args_list if call.args[0] == "/api/v1/scrape-profiles/"]


# ---------------------------------------------------------------------------
# _auto_scrape_all_enabled — the env gate in isolation
# ---------------------------------------------------------------------------


class TestAutoScrapeAllEnabled:
    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        assert it._auto_scrape_all_enabled() is False

    def test_truthy_values_on(self, monkeypatch):
        for val in ("1", "true", "yes", "on", "TRUE", " On "):
            monkeypatch.setenv(FLAG, val)
            assert it._auto_scrape_all_enabled() is True, val

    def test_falsey_values_off(self, monkeypatch):
        for val in ("0", "false", "no", "off", ""):
            monkeypatch.setenv(FLAG, val)
            assert it._auto_scrape_all_enabled() is False, val


# ---------------------------------------------------------------------------
# _create_posts_from_urls — CADDY_AUTO_SCRAPE gating + 201-only idempotency
# ---------------------------------------------------------------------------


class TestCreatePostsAutoScrapeAll:
    def test_gate_off_queues_no_scrape(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        monkeypatch.delenv(KNOWN_GOOD_FLAG, raising=False)
        api = _make_api(jobpost_status=201)
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=URL)]))
        # Post is still created — the scrape is purely additive.
        assert result["created"] == [URL]
        assert result["scrapes_queued"] == 0
        assert _scrape_post_attrs(api) == []

    def test_gate_on_fresh_create_queues_one_hold_scrape(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        monkeypatch.delenv(KNOWN_GOOD_FLAG, raising=False)
        api = _make_api(jobpost_status=201)
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=URL)]))
        assert result["created"] == [URL]
        assert result["scrapes_queued"] == 1
        attrs = _scrape_post_attrs(api)
        assert len(attrs) == 1
        # Plain hold scrape — url + status only, no auto_score (unlike known-good).
        assert attrs[0]["status"] == "hold"
        assert attrs[0]["url"] == URL
        assert "auto_score" not in attrs[0]
        # Carries the job_post_id so the runner AUGMENTS this post (NanoID str).
        rels = _scrape_post_rels(api)
        assert rels[0]["job-post"]["data"]["id"] == POST_ID
        # No dedupe pre-check: the /scrapes/ GET is never called by the broad gate.
        assert all(c.args[0] != "/api/v1/scrapes/" for c in api.get.call_args_list)

    def test_gate_on_dedupe_hit_queues_no_scrape(self, monkeypatch):
        # 200 = api dedupe hit. The 201-only trigger is the idempotency guard,
        # so a dedupe hit must queue nothing (the trap the get_scrapes pre-check
        # would fall into — see api-scrape-viewset-ignores-job-post-filter).
        monkeypatch.setenv(FLAG, "1")
        monkeypatch.delenv(KNOWN_GOOD_FLAG, raising=False)
        api = _make_api(jobpost_status=200)
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=URL)]))
        assert result["duplicates"] == [URL]
        assert result["created"] == []
        assert result["scrapes_queued"] == 0
        assert _scrape_post_attrs(api) == []

    def test_scrape_create_failure_never_fails_triage(self, monkeypatch):
        # A non-success scrape-create envelope must not fail the JobPost triage.
        monkeypatch.setenv(FLAG, "1")
        monkeypatch.delenv(KNOWN_GOOD_FLAG, raising=False)
        api = _make_api(jobpost_status=201, scrape_create_success=False)
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=URL)]))
        assert result["created"] == [URL]
        assert result["failed"] == []
        assert result["scrapes_queued"] == 0

    def test_scrape_create_exception_never_fails_triage(self, monkeypatch):
        # A raised exception from create_scrape must be swallowed (fail-open).
        monkeypatch.setenv(FLAG, "1")
        monkeypatch.delenv(KNOWN_GOOD_FLAG, raising=False)
        api = _make_api(jobpost_status=201, scrape_create_raises=True)
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=URL)]))
        assert result["created"] == [URL]
        assert result["failed"] == []
        assert result["scrapes_queued"] == 0

    def test_explicit_param_overrides_env(self, monkeypatch):
        # auto_scrape_all=False must win even when the env flag is on.
        monkeypatch.setenv(FLAG, "1")
        monkeypatch.delenv(KNOWN_GOOD_FLAG, raising=False)
        api = _make_api(jobpost_status=201)
        result = asyncio.run(
            it._create_posts_from_urls(api, [_Link(url=URL)], auto_scrape_all=False)
        )
        assert result["created"] == [URL]
        assert result["scrapes_queued"] == 0
        assert _scrape_post_attrs(api) == []


# ---------------------------------------------------------------------------
# Precedence — CADDY_AUTO_SCRAPE wins over the known-good gate per post
# ---------------------------------------------------------------------------


def _profile(*, is_known_good: bool, tier: str | None) -> dict:
    return {
        "id": "1",
        "type": "scrape-profile",
        "attributes": {
            "is_known_good": is_known_good,
            "readiness": {"known_good": is_known_good, "tier": tier, "reasons": []},
        },
    }


class TestGatePrecedence:
    def test_both_gates_on_known_good_post_scraped_once(self, monkeypatch):
        # A fresh create on a known-good host with BOTH gates on must get
        # exactly ONE scrape (the broad gate), never two. The broad scrape is a
        # plain hold (no auto_score); the known-good path is skipped, so no
        # readiness lookup happens for that post.
        monkeypatch.setenv(FLAG, "1")
        monkeypatch.setenv(KNOWN_GOOD_FLAG, "1")
        api = _make_api(jobpost_status=201, profile=_profile(is_known_good=True, tier="verified"))
        result = asyncio.run(
            it._create_posts_from_urls(
                api, [_Link(url=URL)], auto_scrape_known_good=True, auto_scrape_all=True
            )
        )
        assert result["created"] == [URL]
        assert result["scrapes_queued"] == 1
        attrs = _scrape_post_attrs(api)
        assert len(attrs) == 1
        assert "auto_score" not in attrs[0]  # broad gate, not the free known-good path
        # Broad gate took precedence → known-good readiness lookup was skipped.
        assert _profile_lookups(api) == []

    def test_known_good_only_still_scrapes_when_broad_off(self, monkeypatch):
        # Broad gate OFF, known-good ON: the AUTO-29 path is untouched — a
        # known-good post still gets its free auto_score=False scrape.
        monkeypatch.delenv(FLAG, raising=False)
        monkeypatch.setenv(KNOWN_GOOD_FLAG, "1")
        api = _make_api(jobpost_status=201, profile=_profile(is_known_good=True, tier="verified"))
        result = asyncio.run(
            it._create_posts_from_urls(
                api, [_Link(url=URL)], auto_scrape_known_good=True, auto_scrape_all=False
            )
        )
        assert result["created"] == [URL]
        assert result["scrapes_queued"] == 1
        attrs = _scrape_post_attrs(api)
        assert len(attrs) == 1
        assert attrs[0]["auto_score"] is False  # the free known-good enrichment
