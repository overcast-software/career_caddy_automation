"""CC-125 — a duplicate-JobPost 409 is a DUPLICATE, not a failure.

The api reports "this link is already a JobPost" two ways: ``200`` + the
existing resource (the merge path), or ``409`` + ``code=duplicate_job_post``
(a canonical collision it declined to merge). The extract loop bucketed only
the first as a duplicate and called the second a failure — so a digest whose
links ALL already exist terminalized as ``new_failed``, never got the
``caddy_processed`` tag, and was re-picked by the ``NOT tag:caddy_processed``
selector on the very next sweep. Re-classify, re-extract, same 409s, forever.

That loop ran for seven weeks at ~24,000 API calls/day — 68% of all production
traffic — and exhausted the GCP trial credit. These tests pin the terminality,
not just the counter.

Two layers, both load-bearing:

* ``_create_posts_from_urls`` buckets the 409 into ``duplicates`` and leaves
  ``failed`` EMPTY. ``failed`` being empty is the exact condition that writes
  ``caddy_processed`` (``inbox_triage.py`` stage-H) — the counter is the cause,
  the tag is the effect.
* ``_triage_one`` end-to-end with the REAL ``_create_posts_from_urls``: an
  all-duplicate email lands on ``new_duplicate`` WITH the tag. Mocking the
  create helper here (as the neighbouring forward-path tests do) would assert
  the fix against a stub of the thing being fixed.

The carve-out is deliberately narrow: a 409 WITHOUT that code is still a
failure, because a genuine conflict deserves the retry ``new_failed`` buys.

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

import scripts.inbox_triage as it
from src.client.api_client import ApiClient
from src.email_source import EmailMeta

JOB_URL = "https://acme.com/jobs/staff-engineer"
OTHER_URL = "https://acme.com/jobs/principal-engineer"
OWNER_ID = 7
# Real NanoID-shaped id — a numeric string would mask an int()-cast regression.
EXISTING_POST_ID = "V30p4hHABQ"


def _envelope(status_code: int, body: dict | str) -> str:
    """Build the envelope the way production does — through the real ``_ok``.

    These fixtures used to hand-assemble the dict, which meant they encoded one
    session's belief about the transport. AUTO-52 changed that transport (a
    non-2xx now carries structured ``errors`` alongside the flattened string),
    and a hand-written fixture would have gone on passing while asserting
    against a shape the client no longer produces. Driving the real ``_ok``
    over a real response means these tests break the day the contract moves.
    """
    if isinstance(body, str):
        response = httpx.Response(status_code, text=body)
    else:
        response = httpx.Response(status_code, json=body)
    return ApiClient("https://example.test", "jh_test")._ok(response)


def _dup_409_envelope() -> str:
    """The api's real duplicate response — job_hunting/api/views/jobs.py:901-920."""
    return _envelope(
        409,
        {
            "errors": [
                {
                    "status": "409",
                    "code": "duplicate_job_post",
                    "detail": (
                        "A job post with this link already exists. Open the existing "
                        "post or re-submit from a higher-trust source."
                    ),
                    "meta": {"job_post_id": EXISTING_POST_ID, "title": "Staff Engineer"},
                }
            ]
        },
    )


def _other_409_envelope() -> str:
    """A 409 that is NOT a duplicate — must still count as a failure."""
    return _envelope(
        409, {"errors": [{"status": "409", "code": "scrape_in_flight", "detail": "busy"}]}
    )


def _server_error_envelope() -> str:
    return _envelope(500, "upstream boom")


@dataclass
class _Link:
    url: str
    title: str = "Staff Engineer"
    company: str | None = None
    description: str | None = None


def _api_posting(*envelopes: str) -> MagicMock:
    """ApiClient stand-in: POST /job-posts/ returns each envelope in turn.

    The users GET resolves any localpart to one CC user so the owner gate lets
    a message through in the end-to-end test.
    """
    api = MagicMock()
    remaining = list(envelopes)

    async def _get(path: str, params: dict | None = None) -> str:
        if path == "/api/v1/users/":
            return json.dumps(
                {"success": True, "data": {"data": [{"id": str(OWNER_ID), "type": "user"}]}}
            )
        # Any other read (scrape-profile readiness, existing scrapes) is a miss.
        return json.dumps({"success": True, "data": {"data": []}, "status_code": 200})

    async def _post(path: str, payload: dict) -> str:
        if path == "/api/v1/job-posts/":
            return remaining.pop(0)
        raise AssertionError(f"unexpected POST {path}")

    api.get = AsyncMock(side_effect=_get)
    api.post = AsyncMock(side_effect=_post)
    return api


# ---------------------------------------------------------------------------
# _is_duplicate_job_post_conflict — the predicate in isolation
# ---------------------------------------------------------------------------


class TestIsDuplicateJobPostConflict:
    def test_duplicate_409_matches(self):
        assert it._is_duplicate_job_post_conflict(json.loads(_dup_409_envelope())) is True

    def test_other_409_does_not_match(self):
        assert it._is_duplicate_job_post_conflict(json.loads(_other_409_envelope())) is False

    def test_non_409_does_not_match(self):
        assert it._is_duplicate_job_post_conflict(json.loads(_server_error_envelope())) is False

    def test_missing_error_string_does_not_match(self):
        # A 409 whose body never made it into the envelope must not be assumed
        # benign — silence is not evidence of a duplicate.
        assert it._is_duplicate_job_post_conflict({"status_code": 409, "error": None}) is False

    def test_codeless_409_does_not_match(self):
        # A 409 the api sent without a `code` — e.g. an edge or middleware
        # answering before any view ran. Nothing asserts it is a duplicate, so
        # it stays a failure and keeps its retry.
        parsed = json.loads(_envelope(409, {"errors": [{"detail": "conflict"}]}))
        assert it._is_duplicate_job_post_conflict(parsed) is False

    def test_non_json_409_does_not_match(self):
        # An HTML conflict page from a proxy yields no structured errors at all.
        parsed = json.loads(_envelope(409, "<html>Conflict</html>"))
        assert it._is_duplicate_job_post_conflict(parsed) is False

    def test_success_envelope_does_not_match(self):
        assert it._is_duplicate_job_post_conflict({"success": True, "status_code": 200}) is False


# ---------------------------------------------------------------------------
# _create_posts_from_urls — the bucketing
# ---------------------------------------------------------------------------


class TestCreatePostsDuplicate409:
    def test_duplicate_409_counts_as_duplicate_not_failure(self, monkeypatch):
        monkeypatch.delenv("CADDY_AUTO_SCRAPE", raising=False)
        monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
        api = _api_posting(_dup_409_envelope())
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=JOB_URL)]))
        assert result["duplicates"] == [JOB_URL]
        assert result["created"] == []
        # THE regression: a non-empty `failed` is what denied the tag.
        assert result["failed"] == []

    def test_all_links_duplicate_leaves_failed_empty(self, monkeypatch):
        monkeypatch.delenv("CADDY_AUTO_SCRAPE", raising=False)
        monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
        api = _api_posting(_dup_409_envelope(), _dup_409_envelope())
        result = asyncio.run(
            it._create_posts_from_urls(api, [_Link(url=JOB_URL), _Link(url=OTHER_URL)])
        )
        assert sorted(result["duplicates"]) == sorted([JOB_URL, OTHER_URL])
        assert result["failed"] == []

    def test_non_duplicate_409_still_fails(self, monkeypatch):
        monkeypatch.delenv("CADDY_AUTO_SCRAPE", raising=False)
        monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
        api = _api_posting(_other_409_envelope())
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=JOB_URL)]))
        assert result["failed"] == [JOB_URL]
        assert result["duplicates"] == []

    def test_server_error_still_fails(self, monkeypatch):
        monkeypatch.delenv("CADDY_AUTO_SCRAPE", raising=False)
        monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
        api = _api_posting(_server_error_envelope())
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=JOB_URL)]))
        assert result["failed"] == [JOB_URL]
        assert result["duplicates"] == []

    def test_duplicate_409_queues_no_scrape(self, monkeypatch):
        # Nothing is scraped or enriched off this path. Note the reason is NOT
        # that the id is unavailable — since AUTO-52 the 409's
        # `errors[0].meta.job_post_id` names the row we collided with. Pinned so
        # that acting on it is a deliberate decision with its own ticket rather
        # than a silent rider on an error-handling change.
        monkeypatch.setenv("CADDY_AUTO_SCRAPE", "1")
        monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
        api = _api_posting(_dup_409_envelope())
        result = asyncio.run(it._create_posts_from_urls(api, [_Link(url=JOB_URL)]))
        assert result["scrapes_queued"] == 0
        assert all(c.args[0] != "/api/v1/scrapes/" for c in api.post.call_args_list)

    def test_mixed_duplicate_and_real_failure_still_fails(self, monkeypatch):
        # One genuine error among duplicates must keep the email retryable —
        # the fix must not blanket-terminalize an email with real work left.
        monkeypatch.delenv("CADDY_AUTO_SCRAPE", raising=False)
        monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
        api = _api_posting(_dup_409_envelope(), _server_error_envelope())
        result = asyncio.run(
            it._create_posts_from_urls(api, [_Link(url=JOB_URL), _Link(url=OTHER_URL)])
        )
        assert result["duplicates"] == [JOB_URL]
        assert result["failed"] == [OTHER_URL]


# ---------------------------------------------------------------------------
# _triage_one end-to-end — the terminality the loop actually depends on
# ---------------------------------------------------------------------------


class _Agent:
    """Minimal pydantic-ai Agent stand-in: ``.run()`` returns ``.output``."""

    def __init__(self, output):
        self._output = output

    async def run(self, *args, **kwargs):
        return SimpleNamespace(output=self._output)


class _FakeSource:
    def __init__(self):
        self.messages = {
            "fwd@dougheadley.com": {
                "thread": "Tsolo",
                "subject": "Fwd: 3 new roles",
                "tags": {"inbox"},
                "recipient": "dough",
            }
        }

    def meta(self, message_id: str) -> EmailMeta:
        m = self.messages[message_id]
        return EmailMeta(
            id=message_id,
            subject=m["subject"],
            tags=set(m["tags"]),
            thread_id=m["thread"],
            recipient=m["recipient"],
        )

    async def add_tags(self, message_id: str, tags: list[str]) -> None:
        self.messages[message_id]["tags"].update(tags)


def test_all_duplicate_email_terminalizes_with_caddy_processed(monkeypatch):
    """The whole point of CC-125, asserted through the real create path.

    Every link comes back 409 duplicate_job_post → the email must land on
    `new_duplicate` AND carry `caddy_processed`, so the NOT-tag selector never
    hands it back. Before the fix this was `new_failed` with no tag, which is
    what made the sweep re-run forever.
    """
    monkeypatch.delenv("CADDY_AUTO_SCRAPE", raising=False)
    monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "digest body with two job links")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(
            return_value=SimpleNamespace(
                job_urls=[_Link(url=JOB_URL), _Link(url=OTHER_URL)],
                reasoning="2 kept",
            )
        ),
    )
    src = _FakeSource()
    api = _api_posting(_dup_409_envelope(), _dup_409_envelope())
    outcome = asyncio.run(
        it._triage_one(
            src.meta("fwd@dougheadley.com"),
            src,
            _Agent("job_post"),
            _Agent(SimpleNamespace(title="", confidence=0.0, evidence="n/a")),
            api,
        )
    )
    assert outcome.outcome == "new_duplicate"
    assert "caddy_processed" in src.messages["fwd@dougheadley.com"]["tags"]
