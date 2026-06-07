"""cc_auto's outbound JobPost create payload carries the catchall
provenance attributes added in api PRs #149/#150:

- ``source="email-forward"``
- ``forwarded_via_address=<localpart>@careercaddy.online``
- ``discover_for_user_id=<resolved user id>``

The api accepts these on both POST paths. cc_auto must:

1. Pass them through when callers set them.
2. Mark ``complete=False`` for ``source="email-forward"`` (added to
   ``_EMAIL_TIER_SOURCES``).
3. Omit them when not set — backwards-compat for the existing inbox
   triage path.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.client.api_client import (
    create_job_post_minimal,
    create_job_post_with_company_check,
    find_user_by_username,
)


def _ok_response(payload: dict | None = None) -> str:
    return json.dumps({"success": True, "data": payload or {"data": {"id": "1"}}})


def _make_api_mock(post_response: dict | None = None) -> MagicMock:
    """Mock ApiClient with async post/get stubs."""
    api = MagicMock()
    api.post = AsyncMock(return_value=_ok_response(post_response))
    api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
    return api


def _last_post_payload(api: MagicMock) -> dict:
    args, _kwargs = api.post.call_args
    return args[1]


def _attrs(api: MagicMock) -> dict:
    return _last_post_payload(api)["data"]["attributes"]


class TestCreateJobPostMinimalForwardProvenance:
    def test_email_forward_carries_provenance_attrs(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_minimal(
                api,
                title="Senior Backend Engineer",
                link="https://acme.com/jobs/1",
                source="email-forward",
                forwarded_via_address="dough@careercaddy.online",
                discover_for_user_id=2,
            )
        )
        attrs = _attrs(api)
        assert attrs["source"] == "email-forward"
        assert attrs["forwarded_via_address"] == "dough@careercaddy.online"
        assert attrs["discover_for_user_id"] == 2

    def test_email_forward_marked_complete_false(self):
        """email-forward joins the email-tier trust set — same as
        ``email`` / ``email_direct``."""
        api = _make_api_mock()
        asyncio.run(
            create_job_post_minimal(
                api,
                title="t",
                link="https://x/1",
                source="email-forward",
            )
        )
        assert _attrs(api).get("complete") is False

    def test_default_path_omits_provenance(self):
        api = _make_api_mock()
        asyncio.run(create_job_post_minimal(api, title="t", link="https://x/1"))
        attrs = _attrs(api)
        assert "forwarded_via_address" not in attrs
        assert "discover_for_user_id" not in attrs


class TestCreateJobPostWithCompanyCheckForwardProvenance:
    @pytest.fixture(autouse=True)
    def _company_search_returns_existing(self, monkeypatch):
        async def fake_find(api, name):
            return json.dumps(
                {
                    "success": True,
                    "data": {"companies": [{"id": "42"}]},
                }
            )

        monkeypatch.setattr("src.client.api_client.find_company_by_name", fake_find)

    def test_email_forward_carries_provenance_attrs(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_with_company_check(
                api,
                title="t",
                company_name="Acme",
                link="https://x/1",
                source="email-forward",
                forwarded_via_address="dough@careercaddy.online",
                discover_for_user_id=2,
            )
        )
        attrs = _attrs(api)
        assert attrs["source"] == "email-forward"
        assert attrs["forwarded_via_address"] == "dough@careercaddy.online"
        assert attrs["discover_for_user_id"] == 2

    def test_email_forward_marked_complete_false(self):
        api = _make_api_mock()
        asyncio.run(
            create_job_post_with_company_check(
                api,
                title="t",
                company_name="Acme",
                link="https://x/1",
                source="email-forward",
            )
        )
        assert _attrs(api).get("complete") is False

    def test_no_provenance_attrs_when_not_set(self):
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
        attrs = _attrs(api)
        assert "forwarded_via_address" not in attrs
        assert "discover_for_user_id" not in attrs


class TestFindUserByUsername:
    def test_hits_users_endpoint_with_username_filter(self):
        api = MagicMock()
        api.get = AsyncMock(
            return_value=json.dumps(
                {
                    "success": True,
                    "data": {"data": [{"id": "2", "type": "user"}]},
                }
            )
        )
        asyncio.run(find_user_by_username(api, "dough"))
        args, kwargs = api.get.call_args
        assert args[0] == "/api/v1/users/"
        assert kwargs["params"] == {"filter[username]": "dough"}
