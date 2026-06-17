"""``fetch_profile_readiness`` reads the api's per-domain scrape-readiness
signal (ScrapeProfile filter endpoint, api PR #185) and returns
``(is_known_good, tier)`` or ``None``.

Contract under test:

1. Hits ``GET /api/v1/scrape-profiles/?filter[hostname]=<host>``.
2. Reads ``data[0].attributes.is_known_good`` (snake_case — the api does
   not dasherize) and ``data[0].attributes.readiness.tier``.
3. Fail-safe: no profile / non-success envelope / api.get raises all
   return ``None`` so the caller treats "unknown" like "not known-good".
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from src.client.api_client import fetch_profile_readiness


def _profiles_envelope(profiles: list[dict]) -> str:
    return json.dumps({"success": True, "data": {"data": profiles}, "status_code": 200})


def _profile(*, is_known_good: bool, tier: str | None) -> dict:
    return {
        "id": "1",
        "type": "scrape-profile",
        "attributes": {
            "is_known_good": is_known_good,
            "readiness": {"known_good": is_known_good, "tier": tier, "reasons": []},
        },
    }


class TestFetchProfileReadiness:
    def test_known_good_hit_returns_tuple(self):
        api = MagicMock()
        api.get = AsyncMock(
            return_value=_profiles_envelope([_profile(is_known_good=True, tier="verified")])
        )
        result = asyncio.run(fetch_profile_readiness(api, "acme.com"))
        assert result == (True, "verified")
        args, kwargs = api.get.call_args
        assert args[0] == "/api/v1/scrape-profiles/"
        assert kwargs["params"] == {"filter[hostname]": "acme.com"}

    def test_not_known_good_hit_returns_false(self):
        api = MagicMock()
        api.get = AsyncMock(
            return_value=_profiles_envelope([_profile(is_known_good=False, tier="emerging")])
        )
        assert asyncio.run(fetch_profile_readiness(api, "acme.com")) == (False, "emerging")

    def test_missing_readiness_yields_none_tier(self):
        api = MagicMock()
        api.get = AsyncMock(
            return_value=_profiles_envelope(
                [{"id": "1", "type": "scrape-profile", "attributes": {"is_known_good": True}}]
            )
        )
        assert asyncio.run(fetch_profile_readiness(api, "acme.com")) == (True, None)

    def test_no_profile_returns_none(self):
        api = MagicMock()
        api.get = AsyncMock(return_value=_profiles_envelope([]))
        assert asyncio.run(fetch_profile_readiness(api, "ghost.example")) is None

    def test_non_success_envelope_returns_none(self):
        api = MagicMock()
        api.get = AsyncMock(
            return_value=json.dumps({"success": False, "error": "403", "status_code": 403})
        )
        assert asyncio.run(fetch_profile_readiness(api, "acme.com")) is None

    def test_api_get_raises_returns_none(self):
        api = MagicMock()
        api.get = AsyncMock(side_effect=RuntimeError("connection reset"))
        assert asyncio.run(fetch_profile_readiness(api, "acme.com")) is None
