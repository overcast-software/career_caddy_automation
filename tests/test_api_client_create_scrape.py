"""``create_scrape`` builds the JSON:API scrape POST payload.

Contract under test:

1. ``attended=True`` adds ``attended: true`` to the POST ``attributes``
   dict (snake_case — the api does not dasherize attribute keys).
2. The default (``attended`` omitted / ``False``) sends a byte-identical
   payload to before — no ``attended`` key at all.
3. ``status`` + the ``job_post`` relationship still ride along unchanged.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from src.client.api_client import create_scrape


def _ok_response() -> str:
    return json.dumps({"success": True, "data": {"data": {"id": "1", "type": "scrape"}}})


def _make_api_mock() -> MagicMock:
    api = MagicMock()
    api.post = AsyncMock(return_value=_ok_response())
    return api


def _post_payload(api: MagicMock) -> dict:
    args, _kwargs = api.post.call_args
    assert args[0] == "/api/v1/scrapes/"
    return args[1]


def _attrs(api: MagicMock) -> dict:
    return _post_payload(api)["data"]["attributes"]


class TestCreateScrapeAttended:
    def test_attended_true_sets_attribute(self):
        api = _make_api_mock()
        asyncio.run(
            create_scrape(
                api, url="https://acme.com/j/1", job_post_id=42, status="hold", attended=True
            )
        )
        attrs = _attrs(api)
        assert attrs["attended"] is True
        assert attrs["status"] == "hold"
        assert attrs["url"] == "https://acme.com/j/1"

    def test_default_omits_attended(self):
        api = _make_api_mock()
        asyncio.run(create_scrape(api, url="https://acme.com/j/1", job_post_id=42, status="hold"))
        assert "attended" not in _attrs(api)

    def test_attended_false_omits_attended(self):
        api = _make_api_mock()
        asyncio.run(
            create_scrape(
                api, url="https://acme.com/j/1", job_post_id=42, status="hold", attended=False
            )
        )
        assert "attended" not in _attrs(api)

    def test_job_post_relationship_still_present_with_attended(self):
        api = _make_api_mock()
        asyncio.run(create_scrape(api, url="https://acme.com/j/1", job_post_id=42, attended=True))
        rels = _post_payload(api)["data"]["relationships"]
        assert rels["job-post"]["data"]["id"] == "42"
