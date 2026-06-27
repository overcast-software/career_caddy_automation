"""Forward-path triage — terminal-tag + message-granularity + dedupe guards.

The forward-only redesign collapsed the old 5-stage ladder into two paths
(extract-links / inline-fallback) behind one cheap classify. Two invariants
keep it from melting tokens or poisoning siblings, and both are pinned here:

* ``caddy_processed`` is written on EVERY terminal path (``not_job``,
  ``new_created``, ``new_no_urls``, ``inline_created``). The pending selector
  is ``NOT tag:caddy_processed`` — a path that forgets the tag re-runs the
  LLMs over the whole backlog every 15-min pass (a token-burn loop).
* every tag read/write is MESSAGE-granular (``meta.id``). A forward sharing a
  thread with an already-processed original is judged on its own state and
  must not re-tag the sibling — the double-post guard is JobPost dedupe
  (canonical_link), not thread-tag skipping.

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import scripts.inbox_triage as it
from src.email_source import EmailMeta

POST_ID = "V30p4hHABQ"  # NanoID-shaped — a numeric string would mask an int()-cast regression.
JOB_URL = "https://acme.com/jobs/frontend-engineer"


@dataclass
class _Link:
    url: str
    title: str = "Frontend Engineer"
    company: str | None = None
    description: str | None = None


class _Agent:
    """Minimal pydantic-ai Agent stand-in: ``.run()`` returns ``.output``."""

    def __init__(self, output):
        self._output = output

    async def run(self, *args, **kwargs):
        return SimpleNamespace(output=self._output)


def _confident_inline() -> SimpleNamespace:
    return SimpleNamespace(
        title="Senior Backend Engineer",
        company="Acme",
        description="Build things.",
        location=None,
        salary_min=None,
        salary_max=None,
        remote_ok=False,
        recruiter_contact=None,
        confidence=0.9,
        evidence="responsibilities include …",
    )


def _thin_inline() -> SimpleNamespace:
    return SimpleNamespace(title="", confidence=0.0, evidence="too thin to stand as a post")


class FakeMessageSource:
    """Message-granular email source. ``add_tags`` mutates ONLY the addressed
    message — and asserts it was handed a real message id, so a regression
    back to ``meta.thread_id`` (thread granularity) fails loudly."""

    def __init__(self, messages: dict[str, dict]):
        self.messages = messages

    def meta(self, message_id: str) -> EmailMeta:
        m = self.messages[message_id]
        return EmailMeta(
            id=message_id, subject=m["subject"], tags=set(m["tags"]), thread_id=m["thread"]
        )

    async def add_tags(self, message_id: str, tags: list[str]) -> None:
        assert message_id in self.messages, (
            f"add_tags got non-message id {message_id!r} — thread-granular regression?"
        )
        self.messages[message_id]["tags"].update(tags)


def _solo(tags: set[str] | None = None) -> FakeMessageSource:
    return FakeMessageSource(
        {
            "fwd@dougheadley.com": {
                "thread": "Tsolo",
                "subject": "Fwd: New role",
                "tags": tags if tags is not None else {"inbox"},
            }
        }
    )


def _run(meta, source, *, classify="job_post", inline=None, api=None):
    classify_agent = _Agent(classify)
    inline_agent = _Agent(inline if inline is not None else _thin_inline())
    return asyncio.run(
        it._triage_one(meta, source, classify_agent, inline_agent, api or AsyncMock())
    )


# ---------------------------------------------------------------------------
# caddy_processed is written on EVERY terminal path (token-burn-loop guard)
# ---------------------------------------------------------------------------


def test_not_job_writes_caddy_processed(monkeypatch):
    src = _solo()
    outcome = _run(src.meta("fwd@dougheadley.com"), src, classify="not_job_post nope")
    assert outcome.outcome == "not_job"
    assert "caddy_processed" in src.messages["fwd@dougheadley.com"]["tags"]
    assert "job_post" not in src.messages["fwd@dougheadley.com"]["tags"]


def test_new_created_writes_caddy_processed(monkeypatch):
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link(url=JOB_URL)], reasoning="1 kept")),
    )
    monkeypatch.setattr(
        it,
        "_create_posts_from_urls",
        AsyncMock(
            return_value={
                "created": [JOB_URL],
                "duplicates": [],
                "failed": [],
                "scrapes_queued": 0,
            }
        ),
    )
    src = _solo()
    outcome = _run(src.meta("fwd@dougheadley.com"), src)
    assert outcome.outcome == "new_created"
    assert "caddy_processed" in src.messages["fwd@dougheadley.com"]["tags"]


def test_new_no_urls_inline_thin_writes_caddy_processed(monkeypatch):
    """No links + an inline result too thin to post → new_no_urls, still
    marked processed so it stops re-matching."""
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "Non-text part: text/html")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[], reasoning="0 kept")),
    )
    src = _solo()
    outcome = _run(src.meta("fwd@dougheadley.com"), src, inline=_thin_inline())
    assert outcome.outcome == "new_no_urls"
    assert "caddy_processed" in src.messages["fwd@dougheadley.com"]["tags"]


def test_inline_created_writes_caddy_processed(monkeypatch):
    """No links + a confident inline JD → a link-less JobPost + processed tag."""
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "JD pasted inline, no link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[], reasoning="0 kept")),
    )
    monkeypatch.setattr(it, "_create_inline_job_post", AsyncMock(return_value="created"))
    src = _solo()
    outcome = _run(src.meta("fwd@dougheadley.com"), src, inline=_confident_inline())
    assert outcome.outcome == "inline_created"
    assert "caddy_processed" in src.messages["fwd@dougheadley.com"]["tags"]


# ---------------------------------------------------------------------------
# message-granularity — a forward with a processed sibling is still triaged
# ---------------------------------------------------------------------------


def _ziprecruiter_forward_source() -> FakeMessageSource:
    """A processed original + an unprocessed forward in one thread. The
    forward's OWN tags are just {"inbox"} (the AUTO-32 list_pending fix)."""
    return FakeMessageSource(
        {
            "orig@ziprecruiter.com": {
                "thread": "T155f3",
                "subject": "Software Engineer, Frontend opening at Red Hook",
                "tags": {"caddy_processed", "evaluated", "inbox", "job_post"},
            },
            "fwd@dougheadley.com": {
                "thread": "T155f3",
                "subject": "Fwd: Software Engineer, Frontend opening at Red Hook",
                "tags": {"inbox"},
            },
        }
    )


def test_forward_with_processed_sibling_is_classified_not_already_done(monkeypatch):
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link(url=JOB_URL)], reasoning="1 kept")),
    )
    monkeypatch.setattr(
        it,
        "_create_posts_from_urls",
        AsyncMock(
            return_value={
                "created": [JOB_URL],
                "duplicates": [],
                "failed": [],
                "scrapes_queued": 0,
            }
        ),
    )
    src = _ziprecruiter_forward_source()
    orig_before = set(src.messages["orig@ziprecruiter.com"]["tags"])

    outcome = _run(src.meta("fwd@dougheadley.com"), src)

    # NOT short-circuited by the poisoned thread union.
    assert outcome.outcome == "new_created"
    fwd_tags = src.messages["fwd@dougheadley.com"]["tags"]
    assert {"evaluated", "job_post", "caddy_processed"} <= fwd_tags
    # The processed original was never re-tagged by triaging the forward.
    assert src.messages["orig@ziprecruiter.com"]["tags"] == orig_before


def test_forward_same_url_as_original_resolves_to_new_duplicate(monkeypatch):
    """When the forward extracts the URL the original already posted, the api
    dedupes (200) → ``new_duplicate`` with exactly ONE create call. This is
    the real double-post guard — NOT thread-tag skipping."""
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link(url=JOB_URL)], reasoning="1 kept")),
    )
    # Real _create_posts_from_urls; only the HTTP create + enrichment are
    # stubbed. status 200 == api canonical_link dedupe hit.
    create_mock = AsyncMock(
        return_value=json.dumps(
            {
                "success": True,
                "status_code": 200,
                "data": {"data": {"id": POST_ID, "attributes": {"canonical_link": JOB_URL}}},
            }
        )
    )
    monkeypatch.setattr(it, "create_job_post_minimal", create_mock)
    monkeypatch.setattr(it, "_enrich_known_good", AsyncMock(return_value="skip"))

    src = _ziprecruiter_forward_source()
    outcome = _run(src.meta("fwd@dougheadley.com"), src)

    assert outcome.outcome == "new_duplicate"
    # Dedupe, not a second JobPost: exactly one create attempt for the one URL.
    assert create_mock.await_count == 1
    assert "caddy_processed" in src.messages["fwd@dougheadley.com"]["tags"]


# ---------------------------------------------------------------------------
# resume checkpoint — an already-evaluated forward skips the classify call
# ---------------------------------------------------------------------------


def test_already_evaluated_skips_classify(monkeypatch):
    """A forward already tagged ``evaluated``/``job_post`` resumes at
    extraction without spending a classify call."""
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link(url=JOB_URL)], reasoning="1 kept")),
    )
    monkeypatch.setattr(
        it,
        "_create_posts_from_urls",
        AsyncMock(
            return_value={
                "created": [JOB_URL],
                "duplicates": [],
                "failed": [],
                "scrapes_queued": 0,
            }
        ),
    )
    src = _solo(tags={"inbox", "evaluated", "job_post"})

    class _BoomAgent:
        async def run(self, *a, **k):
            raise AssertionError("classify must not run when already evaluated")

    outcome = asyncio.run(
        it._triage_one(
            src.meta("fwd@dougheadley.com"), src, _BoomAgent(), _Agent(_thin_inline()), AsyncMock()
        )
    )
    assert outcome.outcome == "new_created"
