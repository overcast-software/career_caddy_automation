"""Unit tests for the B3 catchall poller's pure helpers + process_one.

We never touch IMAP or Mongo here — ``CatchallMessage`` instances are
constructed directly (the imap source's parser is covered in its own
test module), the api is a MagicMock, and ``count_forwards_today`` is
monkeypatched to a fixed return value.

What we DO want to lock down:

1. ``resolve_localpart`` reads ``data.data[0].id`` out of the api
   envelope and returns ``int`` or ``None``.
2. ``_interpret_post_response`` distinguishes 200 (deduped) from
   201/other-2xx (created) and treats success=False as post_failed.
3. ``process_one`` walks parse_failed → unknown_localpart → over_quota
   → no_urls_extracted → created/deduped/post_failed in the right
   order, and threads ``forwarded_via_address`` + ``discover_for_user_id``
   into both create helpers.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.client.api_client import DuplicateCandidate
from src.email_source.imap_source import CatchallMessage
from src.pollers import email_catchall as cc


def _user_lookup_response(user_id: int | None) -> str:
    """Shape the api returns from GET /api/v1/users/?filter[username]=…"""
    if user_id is None:
        data = []
    else:
        data = [{"id": str(user_id), "type": "user"}]
    return json.dumps({"success": True, "data": {"data": data}, "status_code": 200})


def _post_response(*, status_code: int, post_id: int | str = "1") -> str:
    return json.dumps(
        {
            "success": True,
            "data": {"data": {"id": str(post_id), "type": "job-post", "attributes": {}}},
            "status_code": status_code,
        }
    )


def _post_error_response(error: str = "boom", status_code: int = 500) -> str:
    return json.dumps({"success": False, "error": error, "status_code": status_code})


def _msg(
    *,
    uid: str = "1",
    localpart: str | None = "dough",
    body: str = "https://acme.com/jobs/1 — Senior Backend Engineer @ Acme",
) -> CatchallMessage:
    return CatchallMessage(
        uid=uid,
        message_id=f"<{uid}@catchall>",
        subject="fwd: job",
        sender="user@gmail.com",
        to_addresses=[f"{localpart}@careercaddy.online"] if localpart else [],
        body_text=body,
        forwarded_to_localpart=localpart,
        forwarded_via_address=f"{localpart}@careercaddy.online" if localpart else None,
        raw_size=len(body),
    )


# ---------------------------------------------------------------------------
# resolve_localpart
# ---------------------------------------------------------------------------


class TestResolveLocalpart:
    def test_returns_int_user_id_on_match(self, monkeypatch):
        async def fake_find(api, username):
            assert username == "dough"
            return _user_lookup_response(2)

        monkeypatch.setattr(cc, "find_user_by_username", fake_find)
        api = MagicMock()
        result = asyncio.run(cc.resolve_localpart(api, "dough"))
        assert result == 2

    def test_returns_none_on_empty_data(self, monkeypatch):
        async def fake_find(api, username):
            return _user_lookup_response(None)

        monkeypatch.setattr(cc, "find_user_by_username", fake_find)
        result = asyncio.run(cc.resolve_localpart(MagicMock(), "ghost"))
        assert result is None

    def test_returns_none_on_unsuccess(self, monkeypatch):
        async def fake_find(api, username):
            return json.dumps({"success": False, "error": "403"})

        monkeypatch.setattr(cc, "find_user_by_username", fake_find)
        result = asyncio.run(cc.resolve_localpart(MagicMock(), "x"))
        assert result is None

    def test_returns_none_on_json_decode_error(self, monkeypatch):
        async def fake_find(api, username):
            return "not json"

        monkeypatch.setattr(cc, "find_user_by_username", fake_find)
        result = asyncio.run(cc.resolve_localpart(MagicMock(), "x"))
        assert result is None


# ---------------------------------------------------------------------------
# _interpret_post_response
# ---------------------------------------------------------------------------


class TestInterpretPostResponse:
    def test_201_is_created(self):
        outcome, post_id = cc._interpret_post_response(
            _post_response(status_code=201, post_id=10), link="https://x/1"
        )
        assert outcome == "created"
        assert post_id == "10"

    def test_200_is_deduped(self):
        outcome, post_id = cc._interpret_post_response(
            _post_response(status_code=200, post_id=5), link="https://x/1"
        )
        assert outcome == "deduped"
        assert post_id == "5"

    def test_success_false_is_post_failed(self):
        outcome, post_id = cc._interpret_post_response(
            _post_error_response("conflict", status_code=409), link="https://x/1"
        )
        assert outcome == "post_failed"
        assert post_id is None

    def test_garbage_response_is_post_failed(self):
        outcome, post_id = cc._interpret_post_response("not json", link="x")
        assert outcome == "post_failed"
        assert post_id is None


# ---------------------------------------------------------------------------
# process_one
# ---------------------------------------------------------------------------


@dataclass
class _StubLink:
    """Mimics the JobLink shape ``filter_span_atomic`` returns."""

    url: str
    title: str
    company: str = ""
    description: str = ""


@dataclass
class _StubExtracted:
    job_urls: list[_StubLink]
    reasoning: str = ""


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub out url_extractor + span_validator + count_forwards_today
    so process_one is a pure orchestration test.

    Caller customises behaviour by mutating ``state`` before invoking.
    """

    state = {
        "extracted_links": [_StubLink(url="https://acme.com/j/1", title="Senior Backend")],
        "quota_used": 0,
        "user_id": 2,
    }

    async def fake_extract(text, api_token="", pipeline_run_id=None):
        return _StubExtracted(job_urls=list(state["extracted_links"]), reasoning="stub")

    def fake_span_filter(links, body, *, email_id=None):
        # Trust the stub; mimic the real surface (drop nothing).
        return list(links)

    def fake_count_today(user_id):
        return state["quota_used"]

    async def fake_resolve(api, localpart):
        return state["user_id"]

    monkeypatch.setattr(cc, "extract_job_urls", fake_extract)
    monkeypatch.setattr(cc, "filter_span_atomic", fake_span_filter)
    monkeypatch.setattr(cc, "count_forwards_today", fake_count_today)
    monkeypatch.setattr(cc, "resolve_localpart", fake_resolve)
    return state


class TestProcessOne:
    def test_parse_failed_when_no_localpart(self, stub_pipeline):
        msg = _msg(localpart=None)
        out = asyncio.run(cc.process_one(MagicMock(), msg, quota=100))
        assert out.outcome == "parse_failed"
        assert "no catchall-domain recipient" in (out.bounce_reason or "")

    def test_unknown_localpart_when_resolver_returns_none(self, stub_pipeline):
        stub_pipeline["user_id"] = None
        msg = _msg(localpart="ghost")
        out = asyncio.run(cc.process_one(MagicMock(), msg, quota=100))
        assert out.outcome == "unknown_localpart"
        assert "ghost" in (out.bounce_reason or "")

    def test_over_quota_when_count_at_or_above_limit(self, stub_pipeline):
        stub_pipeline["quota_used"] = 100
        msg = _msg()
        out = asyncio.run(cc.process_one(MagicMock(), msg, quota=100))
        assert out.outcome == "over_quota"
        assert out.quota_remaining == 0

    def test_no_urls_when_extractor_drops_all_links(self, stub_pipeline):
        stub_pipeline["extracted_links"] = []
        msg = _msg()
        out = asyncio.run(cc.process_one(MagicMock(), msg, quota=100))
        assert out.outcome == "no_urls_extracted"

    def test_created_path_threads_provenance_into_post(self, stub_pipeline):
        """The forwarded_via_address + discover_for_user_id arrive at
        ``create_job_post_minimal`` (no company on the stub link)."""
        api = MagicMock()
        api.post = AsyncMock(return_value=_post_response(status_code=201, post_id=42))
        api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
        msg = _msg(localpart="dough")
        out = asyncio.run(cc.process_one(api, msg, quota=100))
        assert out.outcome == "created"
        assert out.job_post_id == "42"
        # Inspect the POST payload — provenance attrs landed.
        post_call_payload = api.post.call_args[0][1]
        attrs = post_call_payload["data"]["attributes"]
        assert attrs["source"] == "email-forward"
        assert attrs["forwarded_via_address"] == "dough@careercaddy.online"
        assert attrs["discover_for_user_id"] == 2
        # email-forward → email-tier → complete=False
        assert attrs["complete"] is False

    def test_company_link_uses_with_company_check_path(self, stub_pipeline, monkeypatch):
        """When the extractor surfaces a company name, the poller
        routes through ``create_job_post_with_company_check`` — which
        we patch to a recording stub so we don't need find_company."""
        stub_pipeline["extracted_links"] = [
            _StubLink(url="https://acme.com/j/1", title="Eng", company="Acme")
        ]

        recorded = {}

        async def fake_with_company(api, **kwargs):
            recorded.update(kwargs)
            return _post_response(status_code=201, post_id=7)

        monkeypatch.setattr(cc, "create_job_post_with_company_check", fake_with_company)
        api = MagicMock()
        msg = _msg(localpart="dough")
        out = asyncio.run(cc.process_one(api, msg, quota=100))
        assert out.outcome == "created"
        assert recorded["source"] == "email-forward"
        assert recorded["forwarded_via_address"] == "dough@careercaddy.online"
        assert recorded["discover_for_user_id"] == 2
        assert recorded["company_name"] == "Acme"

    def test_deduped_when_api_returns_200(self, stub_pipeline):
        api = MagicMock()
        api.post = AsyncMock(return_value=_post_response(status_code=200, post_id=99))
        api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
        msg = _msg()
        out = asyncio.run(cc.process_one(api, msg, quota=100))
        assert out.outcome == "deduped"
        assert out.job_post_id == "99"
        assert out.created == 0
        assert out.deduped == 1

    def test_post_failed_when_all_links_fail(self, stub_pipeline):
        api = MagicMock()
        api.post = AsyncMock(return_value=_post_error_response("boom"))
        api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
        msg = _msg()
        out = asyncio.run(cc.process_one(api, msg, quota=100))
        assert out.outcome == "post_failed"
        assert out.failed == 1
        assert out.created == 0


# ---------------------------------------------------------------------------
# Known-good auto-scrape exception (opt-in: CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD)
# ---------------------------------------------------------------------------


def _scrape_response(scrape_id: int | str = "555") -> str:
    return json.dumps(
        {
            "success": True,
            "data": {"data": {"id": str(scrape_id), "type": "scrape", "attributes": {}}},
            "status_code": 201,
        }
    )


@pytest.fixture
def created_post_api():
    """An api MagicMock whose POST always returns a fresh-create (201)."""
    api = MagicMock()
    api.post = AsyncMock(return_value=_post_response(status_code=201, post_id=42))
    api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
    return api


class TestForwardAutoScrape:
    def test_known_good_flag_on_creates_hold_scrape(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """known-good host + flag ON → create_scrape called once with
        status='hold' + the job_post_id; outcome carries scrape_created +
        scrape_id + tier (the audit row's source-of-truth)."""
        monkeypatch.setenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "true")

        async def fake_readiness(api, hostname):
            assert hostname == "acme.com"
            return (True, "verified")

        scrape_calls = []

        async def fake_create_scrape(api, **kwargs):
            scrape_calls.append(kwargs)
            return _scrape_response(555)

        monkeypatch.setattr(cc, "fetch_profile_readiness", fake_readiness)
        monkeypatch.setattr(cc, "create_scrape", fake_create_scrape)

        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert len(scrape_calls) == 1
        assert scrape_calls[0]["status"] == "hold"
        assert scrape_calls[0]["job_post_id"] == 42
        assert scrape_calls[0]["url"] == "https://acme.com/j/1"
        assert out.scrape_created is True
        assert out.scrape_id == 555
        assert out.profile_tier == "verified"

    def test_known_good_flag_off_skips_scrape(self, stub_pipeline, created_post_api, monkeypatch):
        """known-good host + flag OFF → no scrape; readiness never even
        consulted; audit fields stay falsey."""
        monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)

        readiness_calls = []
        scrape_calls = []

        async def fake_readiness(api, hostname):
            readiness_calls.append(hostname)
            return (True, "verified")

        async def fake_create_scrape(api, **kwargs):
            scrape_calls.append(kwargs)
            return _scrape_response()

        monkeypatch.setattr(cc, "fetch_profile_readiness", fake_readiness)
        monkeypatch.setattr(cc, "create_scrape", fake_create_scrape)

        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert readiness_calls == []
        assert scrape_calls == []
        assert out.scrape_created is False
        assert out.scrape_id is None
        assert out.profile_tier is None

    def test_not_known_good_flag_on_skips_scrape(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """not-known-good host + flag ON → no scrape, JobPost kept."""
        monkeypatch.setenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "1")

        scrape_calls = []

        async def fake_readiness(api, hostname):
            return (False, "emerging")

        async def fake_create_scrape(api, **kwargs):
            scrape_calls.append(kwargs)
            return _scrape_response()

        monkeypatch.setattr(cc, "fetch_profile_readiness", fake_readiness)
        monkeypatch.setattr(cc, "create_scrape", fake_create_scrape)

        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert out.job_post_id == "42"
        assert scrape_calls == []
        assert out.scrape_created is False

    def test_profile_fetch_raises_keeps_jobpost(self, stub_pipeline, created_post_api, monkeypatch):
        """profile fetch raises → no scrape, JobPost still recorded, no
        exception escapes process_one."""
        monkeypatch.setenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "on")

        scrape_calls = []

        async def boom(api, hostname):
            raise RuntimeError("api 500")

        async def fake_create_scrape(api, **kwargs):
            scrape_calls.append(kwargs)
            return _scrape_response()

        monkeypatch.setattr(cc, "fetch_profile_readiness", boom)
        monkeypatch.setattr(cc, "create_scrape", fake_create_scrape)

        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert out.job_post_id == "42"
        assert scrape_calls == []
        assert out.scrape_created is False

    def test_deduped_does_not_auto_scrape(self, stub_pipeline, monkeypatch):
        """Quota interaction: dedupes (api 200) must never auto-scrape,
        even with a known-good host + flag ON."""
        monkeypatch.setenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "true")

        scrape_calls = []

        async def fake_readiness(api, hostname):
            return (True, "verified")

        async def fake_create_scrape(api, **kwargs):
            scrape_calls.append(kwargs)
            return _scrape_response()

        monkeypatch.setattr(cc, "fetch_profile_readiness", fake_readiness)
        monkeypatch.setattr(cc, "create_scrape", fake_create_scrape)

        api = MagicMock()
        api.post = AsyncMock(return_value=_post_response(status_code=200, post_id=99))
        api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))

        out = asyncio.run(cc.process_one(api, _msg(), quota=100))

        assert out.outcome == "deduped"
        assert scrape_calls == []
        assert out.scrape_created is False

    def test_attended_flag_on_marks_scrape_attended(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """auto-scrape ON + attended ON + known-good host →
        create_scrape called with attended=True."""
        monkeypatch.setenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "true")
        monkeypatch.setenv("CADDY_FORWARD_ATTENDED_KNOWN_GOOD", "true")

        scrape_calls = []

        async def fake_readiness(api, hostname):
            return (True, "verified")

        async def fake_create_scrape(api, **kwargs):
            scrape_calls.append(kwargs)
            return _scrape_response(555)

        monkeypatch.setattr(cc, "fetch_profile_readiness", fake_readiness)
        monkeypatch.setattr(cc, "create_scrape", fake_create_scrape)

        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert len(scrape_calls) == 1
        assert scrape_calls[0]["attended"] is True
        assert out.scrape_created is True

    def test_attended_flag_off_marks_scrape_unattended(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """auto-scrape ON + attended OFF (today's default) → scrape still
        created, but attended=False — the generic FIFO hold queue."""
        monkeypatch.setenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "true")
        monkeypatch.delenv("CADDY_FORWARD_ATTENDED_KNOWN_GOOD", raising=False)

        scrape_calls = []

        async def fake_readiness(api, hostname):
            return (True, "verified")

        async def fake_create_scrape(api, **kwargs):
            scrape_calls.append(kwargs)
            return _scrape_response(555)

        monkeypatch.setattr(cc, "fetch_profile_readiness", fake_readiness)
        monkeypatch.setattr(cc, "create_scrape", fake_create_scrape)

        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert len(scrape_calls) == 1
        assert scrape_calls[0]["attended"] is False
        assert out.scrape_created is True

    def test_audit_msg_threads_scrape_fields(self, monkeypatch):
        """_audit_msg forwards the outcome's scrape decision to
        record_forward_audit so the forward_audit doc is observable."""
        recorded = {}

        def fake_record(**kwargs):
            recorded.update(kwargs)

        monkeypatch.setattr(cc, "record_forward_audit", fake_record)
        outcome = cc.ProcessOutcome(
            outcome="created",
            job_post_id="42",
            scrape_created=True,
            scrape_id=555,
            profile_tier="verified",
        )
        cc._audit_msg(_msg(), 2, outcome)

        assert recorded["scrape_created"] is True
        assert recorded["scrape_id"] == 555
        assert recorded["profile_tier"] == "verified"


# ---------------------------------------------------------------------------
# Operator-side near-dupe pre-check (CADDY_FORWARD_DEDUPE_SKIP_HIGH)
# ---------------------------------------------------------------------------


def _high(post_id: int = 99) -> DuplicateCandidate:
    return DuplicateCandidate(
        id=post_id,
        title="ID.me Authentication Engineer",
        company_name="ID.me",
        confidence="high",
        match_signals=["title_exact"],
        frontend_url=f"/job-posts/{post_id}",
    )


def _medium(post_id: int = 77) -> DuplicateCandidate:
    return DuplicateCandidate(
        id=post_id,
        title="ID.me Authentication Engineer II",
        company_name="ID.me",
        confidence="medium",
        match_signals=["title_similarity"],
        frontend_url=f"/job-posts/{post_id}",
    )


class TestDedupePrecheck:
    def test_unique_when_no_candidates(self, stub_pipeline, created_post_api, monkeypatch):
        """No candidate → create as usual, dup_decision='unique'."""

        async def fake_dupes(api, **kwargs):
            return []

        monkeypatch.setattr(cc, "find_duplicate_candidates", fake_dupes)
        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert out.created == 1
        assert out.dup_decision == "unique"
        assert out.dup_candidate_of == []
        assert out.dup_skipped == 0
        assert out.dup_flagged == 0

    def test_high_confidence_skip_off_creates_and_flags(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """Fail-open default: a high-confidence hit STILL creates the post
        (never silently dropped) and records dup_decision='suspected-duplicate'."""
        monkeypatch.delenv("CADDY_FORWARD_DEDUPE_SKIP_HIGH", raising=False)

        async def fake_dupes(api, **kwargs):
            return [_high(99)]

        monkeypatch.setattr(cc, "find_duplicate_candidates", fake_dupes)
        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert out.created == 1
        assert created_post_api.post.await_count == 1  # the POST actually happened
        assert out.dup_decision == "suspected-duplicate"
        assert out.dup_candidate_of == [99]
        assert out.dup_skipped == 0
        assert out.dup_flagged == 1

    def test_high_confidence_skip_on_suppresses_create(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """Opt-in skip ON: a high-confidence hit suppresses the POST and
        records dup_decision='skipped-dupe' (no create)."""
        monkeypatch.setenv("CADDY_FORWARD_DEDUPE_SKIP_HIGH", "true")

        async def fake_dupes(api, **kwargs):
            return [_high(99)]

        monkeypatch.setattr(cc, "find_duplicate_candidates", fake_dupes)
        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        # All links skipped → folded into the deduped bucket (ackable).
        assert out.outcome == "deduped"
        assert out.created == 0
        assert created_post_api.post.await_count == 0  # the POST was suppressed
        assert out.dup_decision == "skipped-dupe"
        assert out.dup_candidate_of == [99]
        assert out.dup_skipped == 1

    def test_medium_confidence_creates_and_flags_even_with_skip_on(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """The skip gate only suppresses HIGH-confidence hits. A medium
        near-dupe is always created + flagged, even when skip is ON."""
        monkeypatch.setenv("CADDY_FORWARD_DEDUPE_SKIP_HIGH", "true")

        async def fake_dupes(api, **kwargs):
            return [_medium(77)]

        monkeypatch.setattr(cc, "find_duplicate_candidates", fake_dupes)
        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert out.created == 1
        assert created_post_api.post.await_count == 1
        assert out.dup_decision == "possible-near-dupe"
        assert out.dup_candidate_of == [77]
        assert out.dup_skipped == 0
        assert out.dup_flagged == 1

    def test_precheck_error_fails_open_and_creates(
        self, stub_pipeline, created_post_api, monkeypatch
    ):
        """A lookup that RAISES must not block the create — fail OPEN with
        dup_decision='dup-check-error'."""
        monkeypatch.setenv("CADDY_FORWARD_DEDUPE_SKIP_HIGH", "true")

        async def boom(api, **kwargs):
            raise RuntimeError("dedupe endpoint 500")

        monkeypatch.setattr(cc, "find_duplicate_candidates", boom)
        out = asyncio.run(cc.process_one(created_post_api, _msg(), quota=100))

        assert out.outcome == "created"
        assert out.created == 1
        assert created_post_api.post.await_count == 1
        assert out.dup_decision == "dup-check-error"
        assert out.dup_skipped == 0

    def test_audit_msg_threads_dup_fields(self, monkeypatch):
        """_audit_msg forwards dup_decision + dup_candidate_of to
        record_forward_audit so the decision is observable."""
        recorded = {}

        def fake_record(**kwargs):
            recorded.update(kwargs)

        monkeypatch.setattr(cc, "record_forward_audit", fake_record)
        outcome = cc.ProcessOutcome(
            outcome="created",
            job_post_id="42",
            dup_decision="suspected-duplicate",
            dup_candidate_of=[99, 100],
            dup_flagged=1,
        )
        cc._audit_msg(_msg(), 2, outcome)

        assert recorded["dup_decision"] == "suspected-duplicate"
        assert recorded["dup_candidate_of"] == [99, 100]
