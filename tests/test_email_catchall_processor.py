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
