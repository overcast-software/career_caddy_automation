"""AUTO-52 — ``process_tagged`` stops treating every 409 as a duplicate.

``caddy-process`` is the legacy email path (``caddy-inbox`` subsumes it), but it
runs the same create calls and it had the *opposite* half of the CC-125 mistake.
``inbox_triage`` called every 409 a failure and looped forever. This script
called every 409 a duplicate:

    is_duplicate = resp.get("status_code") in (200, 409) or ...

So a genuine conflict — anything the api might refuse for a reason other than
"already exists" — was counted as a benign duplicate, the email was tagged
processed, and the work was dropped silently. Failing loudly and looping is bad;
succeeding quietly and losing the email is worse, and leaves nothing to find.

Telling the two apart needs the api's ``code``, which the transport only started
preserving in AUTO-52. The rule now matches ``inbox_triage``: a 409 is a
duplicate when the api says ``duplicate_job_post``, and otherwise it is a
failure that keeps its retry.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import scripts.process_tagged as pt
from src.client.api_client import ApiClient

JOB_URL = "https://acme.com/jobs/staff-engineer"
EMAIL_ID = "fwd@dougheadley.com"
EXISTING_POST_ID = "V30p4hHABQ"


def _envelope(status_code: int, body: dict | str) -> str:
    """Through the real ``_ok`` — see test_api_client_structured_errors."""
    if isinstance(body, str):
        response = httpx.Response(status_code, text=body)
    else:
        response = httpx.Response(status_code, json=body)
    return ApiClient("https://example.test", "jh_test")._ok(response)


def _dup_409() -> str:
    return _envelope(
        409,
        {
            "errors": [
                {
                    "status": "409",
                    "code": "duplicate_job_post",
                    "detail": "A job post with this link already exists.",
                    "meta": {"job_post_id": EXISTING_POST_ID},
                }
            ]
        },
    )


def _other_409() -> str:
    return _envelope(409, {"errors": [{"status": "409", "code": "scrape_in_flight"}]})


def _merged_200() -> str:
    return _envelope(200, {"data": {"id": EXISTING_POST_ID, "type": "job-post"}})


def _created_201() -> str:
    return _envelope(201, {"data": {"id": EXISTING_POST_ID, "type": "job-post"}})


@dataclass
class _Link:
    url: str = JOB_URL
    title: str = "Staff Engineer"
    company: str | None = None
    description: str | None = None


@pytest.fixture
def harness(monkeypatch):
    """Stub everything around the create loop; keep the branching real."""
    monkeypatch.setattr(pt, "load_email_text", lambda _id: "body with one job link")
    monkeypatch.setattr(
        pt,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link()], reasoning="1 kept")),
    )
    monkeypatch.setattr(pt, "filter_span_atomic", lambda urls, text, email_id=None: urls)
    monkeypatch.setattr(pt, "_auto_scrape_enabled", lambda: False)

    tagged: list[str] = []
    monkeypatch.setattr(pt, "tag_processed", lambda email_id: tagged.append(email_id))
    return tagged


def _run(envelope: str) -> dict:
    api = MagicMock()
    api.post = AsyncMock(return_value=envelope)
    api.get = AsyncMock(return_value=json.dumps({"success": True, "data": {"data": []}}))
    return asyncio.run(pt.process_single_email(EMAIL_ID, api))


def test_duplicate_409_is_a_duplicate_and_terminalizes(harness):
    result = _run(_dup_409())
    assert result["success"] is True
    assert result["duplicates"] == 1
    assert result["created"] == 0
    # Terminal: the email is tagged, so the pending selector never returns it.
    assert harness == [EMAIL_ID]


def test_non_duplicate_409_is_a_failure_and_stays_retryable(harness):
    """THE regression. This used to come back success=True, tagged, discarded."""
    result = _run(_other_409())
    assert result["success"] is False
    assert result["duplicates"] == 0
    assert "leaving untagged for retry" in result["error"]
    assert harness == []


def test_codeless_409_is_a_failure(harness):
    # Nothing asserted "already exists", so nothing may be assumed benign.
    result = _run(_envelope(409, {"errors": [{"detail": "conflict"}]}))
    assert result["success"] is False
    assert harness == []


def test_merge_200_is_still_a_duplicate(harness):
    result = _run(_merged_200())
    assert result["success"] is True
    assert result["duplicates"] == 1
    assert harness == [EMAIL_ID]


def test_fresh_201_is_still_a_create(harness):
    result = _run(_created_201())
    assert result["success"] is True
    assert result["created"] == 1
    assert result["duplicates"] == 0
    assert harness == [EMAIL_ID]


def test_server_error_is_still_a_failure(harness):
    result = _run(_envelope(500, "upstream boom"))
    assert result["success"] is False
    assert harness == []
