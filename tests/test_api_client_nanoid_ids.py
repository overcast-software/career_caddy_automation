"""NanoID regression guard for the JobPost-create path (AUTO-28).

Since CC-77 swapped Company + JobPost (and every other job-hunting
model) PK to NanoID strings, cc_auto must NOT ``int()`` an api-returned
id. The pre-fix code cast the company id to ``int`` in
``create_job_post_with_company_check`` (both the existing-company and
newly-created-company branches), which raised
``invalid literal for int() with base 10: 'V30p4hHABQ'`` and silently
killed every forwarded job-post create.

The earlier tests used a numeric-string id (``"42"``), so ``int()``
parsed cleanly and the bug stayed hidden — these tests use real NanoID
strings to lock the regression out.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.client.api_client import (
    JobPostCreate,
    create_job_post_with_company_check,
)


def _ok_response(payload: dict | None = None) -> str:
    return json.dumps({"success": True, "data": payload or {"data": {"id": "post01"}}})


def _make_api_mock() -> MagicMock:
    api = MagicMock()
    api.post = AsyncMock(return_value=_ok_response())
    api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
    return api


def _last_post_payload(api: MagicMock) -> dict:
    args, _kwargs = api.post.call_args
    return args[1]


def _company_rel_id(api: MagicMock) -> str:
    return _last_post_payload(api)["data"]["relationships"]["company"]["data"]["id"]


class TestCreateJobPostWithCompanyCheckNanoID:
    def test_existing_company_nanoid_id_rides_into_relationship(self, monkeypatch):
        """Existing-company branch (was api_client.py:355 int() cast)."""
        nano = "V30p4hHABQ"

        async def fake_find(api, name):
            return json.dumps({"success": True, "data": {"companies": [{"id": nano}]}})

        monkeypatch.setattr("src.client.api_client.find_company_by_name", fake_find)
        api = _make_api_mock()

        raw = asyncio.run(
            create_job_post_with_company_check(
                api,
                title="Staff Engineer",
                company_name="Acme",
                link="https://x/1",
                source="email",
            )
        )

        resp = json.loads(raw)
        # Pre-fix: int(nano) raised, the helper swallowed it into a
        # success=False envelope and never POSTed. Post-fix: clean POST.
        assert resp["success"] is True
        api.post.assert_awaited_once()
        assert _company_rel_id(api) == nano

    def test_new_company_nanoid_id_rides_into_relationship(self, monkeypatch):
        """Newly-created-company branch (was api_client.py:370 int() cast)."""
        nano = "a1fFQQe1xV"

        async def fake_find(api, name):
            return json.dumps({"success": True, "data": {"companies": []}})

        async def fake_create(api, **kwargs):
            return json.dumps({"success": True, "data": {"data": {"id": nano}}})

        monkeypatch.setattr("src.client.api_client.find_company_by_name", fake_find)
        monkeypatch.setattr("src.client.api_client.create_company", fake_create)
        api = _make_api_mock()

        raw = asyncio.run(
            create_job_post_with_company_check(
                api,
                title="Staff Engineer",
                company_name="BrandNewCo",
                link="https://x/2",
                source="email",
            )
        )

        resp = json.loads(raw)
        assert resp["success"] is True
        api.post.assert_awaited_once()
        assert _company_rel_id(api) == nano


class TestJobPostCreateModel:
    def test_accepts_nanoid_company_id(self):
        jp = JobPostCreate(title="t", company_id="V30p4hHABQ")
        assert jp.company_id == "V30p4hHABQ"

    def test_rejects_empty_company_id(self):
        with pytest.raises(ValidationError):
            JobPostCreate(title="t", company_id="")
