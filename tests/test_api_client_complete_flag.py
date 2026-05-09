"""cc_auto's outbound JobPost create payload declares complete=False
for email-tier sources (PR-B, posture E).

Mirrors the api-side gate in
``api/job_hunting/api/views/jobs.py::JobPostViewSet.create``. The api
honors inbound ``complete=False`` only when the source is email-tier;
this side declares it on every email-source create so the row lands
flagged and routes through the existing incomplete-recovery pipeline.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.client.api_client import (
    create_job_post_minimal,
    create_job_post_with_company_check,
)


def _ok_response(payload: dict | None = None) -> str:
    return json.dumps({"success": True, "data": payload or {"data": {"id": "1"}}})


def _make_api_mock(post_response: dict | None = None) -> MagicMock:
    """Mock ApiClient with async post/get/find_company stubs."""
    api = MagicMock()
    api.post = AsyncMock(return_value=_ok_response(post_response))
    api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
    return api


def _last_post_payload(api: MagicMock) -> dict:
    args, _kwargs = api.post.call_args
    return args[1]


def _attrs(api: MagicMock) -> dict:
    return _last_post_payload(api)["data"]["attributes"]


class TestCreateJobPostMinimal:
    def test_email_source_sends_complete_false(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_minimal(api, title="t", link="https://x/1", source="email")
        )
        assert _attrs(api).get("complete") is False

    def test_email_direct_source_sends_complete_false(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_minimal(
                api, title="t", link="https://x/1", source="email_direct"
            )
        )
        assert _attrs(api).get("complete") is False

    def test_default_source_sends_complete_false(self):
        api = _make_api_mock()
        # Default source is "email" — the helper's primary caller.
        asyncio.run(create_job_post_minimal(api, title="t", link="https://x/1"))
        assert _attrs(api).get("complete") is False

    def test_chat_source_omits_complete(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_minimal(api, title="t", link="https://x/1", source="chat")
        )
        assert "complete" not in _attrs(api)

    def test_paste_source_omits_complete(self):
        # Paste is high-trust; the api would drop inbound complete=False
        # anyway, but cc_auto shouldn't even ask for it.
        api = _make_api_mock()
        asyncio.run(
            create_job_post_minimal(api, title="t", link="https://x/1", source="paste")
        )
        assert "complete" not in _attrs(api)


class TestCreateJobPostWithCompanyCheck:
    @pytest.fixture(autouse=True)
    def _company_search_returns_existing(self, monkeypatch):
        """Stub find_company_by_name so the test never needs to hit the
        company-create path (and never raises on a missing one)."""

        async def fake_find(api, name):
            return json.dumps({
                "success": True,
                "data": {"companies": [{"id": "42"}]},
            })

        monkeypatch.setattr(
            "src.client.api_client.find_company_by_name", fake_find
        )

    def test_email_source_sends_complete_false(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_with_company_check(
                api,
                title="t",
                company_name="Acme",
                link="https://x/1",
                source="email",
            )
        )
        assert _attrs(api).get("complete") is False

    def test_chat_source_omits_complete(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_with_company_check(
                api,
                title="t",
                company_name="Acme",
                link="https://x/1",
                source="chat",
            )
        )
        assert "complete" not in _attrs(api)

    def test_extension_source_omits_complete(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_with_company_check(
                api,
                title="t",
                company_name="Acme",
                link="https://x/1",
                source="extension",
            )
        )
        assert "complete" not in _attrs(api)

    def test_email_direct_source_sends_complete_false(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_with_company_check(
                api,
                title="t",
                company_name="Acme",
                link="https://x/1",
                source="email_direct",
            )
        )
        assert _attrs(api).get("complete") is False
