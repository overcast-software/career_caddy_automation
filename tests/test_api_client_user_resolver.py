"""``find_user_by_username`` — the staff-only username→user-id resolver.

cc_auto looks a user up by exact ``username`` via
``GET /api/v1/users/?filter[username]=…`` (staff-only, api PR #151).
Generic client helper, kept independent of any one caller — the
forwarding@/notmuch ingest path (and future flows) resolve a localpart
to a Career Caddy user id through it.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from src.client.api_client import find_user_by_username


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

    def test_match_returns_user_in_envelope(self):
        """A hit comes back as ``data.data == [user]`` — the caller reads
        ``users[0]["id"]`` for the owner id."""
        api = MagicMock()
        api.get = AsyncMock(
            return_value=json.dumps(
                {"success": True, "data": {"data": [{"id": "7", "type": "user"}]}}
            )
        )
        resp = json.loads(asyncio.run(find_user_by_username(api, "dough")))
        users = (resp.get("data") or {}).get("data") or []
        assert [u["id"] for u in users] == ["7"]

    def test_unknown_username_returns_empty_list_not_raise(self):
        """The contract cc_auto's owner gate depends on: a syntactically valid
        but unknown username yields an empty list — never an exception — so the
        gate can treat it as a definitive no-user verdict."""
        api = MagicMock()
        api.get = AsyncMock(
            return_value=json.dumps({"success": True, "data": {"data": []}})
        )
        resp = json.loads(asyncio.run(find_user_by_username(api, "ghost")))
        assert resp["success"] is True
        assert (resp.get("data") or {}).get("data") == []
