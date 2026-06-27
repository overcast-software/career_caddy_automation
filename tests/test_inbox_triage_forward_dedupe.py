"""AUTO-32 — a forwarded job sharing a thread with a processed original
must still be triaged, and the same-job double-post must be prevented by
JobPost dedupe (canonical_link), NOT by thread-tag skipping.

Two layers are pinned here:

* ``_triage_one`` routes on the matched MESSAGE: given a forward whose own
  tags are just ``{"inbox"}`` (the AUTO-32 fix to ``list_pending``), it is
  classified → refined → posted, NOT short-circuited to ``already_done``.
  And it tags the forward (``meta.id``), never its thread — so a processed
  sibling is left untouched.
* The double-post guard is dedupe: when the forward extracts the same URL
  the original already posted, the api returns 200 (dedupe hit) and the
  email resolves to ``new_duplicate`` with NO second JobPost create.

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
from src.agents.email_agents import RefineResult
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


class FakeMessageSource:
    """Message-granular email source. Each MESSAGE owns its tag set and shares
    a thread with siblings. ``add_tags`` mutates ONLY the addressed message —
    and asserts it was handed a real message id, so a regression back to
    ``meta.thread_id`` (thread granularity) fails loudly.
    """

    def __init__(self, messages: dict[str, dict]):
        self.messages = messages

    def meta(self, message_id: str) -> EmailMeta:
        m = self.messages[message_id]
        return EmailMeta(
            id=message_id,
            subject=m["subject"],
            tags=set(m["tags"]),
            thread_id=m["thread"],
        )

    async def add_tags(self, message_id: str, tags: list[str]) -> None:
        assert message_id in self.messages, (
            f"add_tags got non-message id {message_id!r} — thread-granular regression?"
        )
        self.messages[message_id]["tags"].update(tags)


def _new_post_agents():
    """classify=job, refine=new_post (high confidence); follow-up/inline unused."""
    return {
        "classify_agent": _Agent("job_post"),
        "refine_agent": _Agent(
            RefineResult(kind="new_post", confidence=0.95, evidence="link to a role")
        ),
        "followup_agent": _Agent(None),
        "inline_post_agent": _Agent(None),
    }


def _ziprecruiter_forward_source() -> FakeMessageSource:
    """The live AUTO-32 shape: a processed original + an unprocessed forward
    in one thread. The forward's OWN tags are just {"inbox"}."""
    return FakeMessageSource(
        {
            "orig@ziprecruiter.com": {
                "thread": "T155f3",
                "subject": "Software Engineer, Frontend opening at Red Hook",
                "tags": {"caddy_processed", "evaluated", "inbox", "job_post", "passed", "refined"},
            },
            "fwd@dougheadley.com": {
                "thread": "T155f3",
                "subject": "Fwd: Software Engineer, Frontend opening at Red Hook",
                "tags": {"inbox"},
            },
        }
    )


def _run_triage(meta, source, agents, api):
    return asyncio.run(
        it._triage_one(
            meta,
            source,
            agents["classify_agent"],
            agents["refine_agent"],
            agents["followup_agent"],
            agents["inline_post_agent"],
            api,
            SimpleNamespace(),  # deps — unused on the new_post path
        )
    )


# ---------------------------------------------------------------------------
# Scenario 1 + 2 + 4 — forward classified, sibling untouched, gets evaluated
# ---------------------------------------------------------------------------


def test_forward_with_processed_sibling_is_classified_not_already_done(monkeypatch):
    monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link(url=JOB_URL)], reasoning="found")),
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

    outcome = _run_triage(src.meta("fwd@dougheadley.com"), src, _new_post_agents(), AsyncMock())

    # NOT short-circuited by the poisoned thread union.
    assert outcome.outcome == "new_created"
    fwd_tags = src.messages["fwd@dougheadley.com"]["tags"]
    assert {"evaluated", "job_post", "refined", "caddy_processed"} <= fwd_tags
    # The processed original was never re-tagged by triaging the forward.
    assert src.messages["orig@ziprecruiter.com"]["tags"] == orig_before


def test_genuinely_new_single_forward_creates_and_earns_evaluated(monkeypatch):
    """A standalone forward (no sibling) → new_created and gets its own
    ``evaluated``/``caddy_processed`` so it never re-matches."""
    monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link(url=JOB_URL)], reasoning="found")),
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

    src = FakeMessageSource(
        {"solo@dougheadley.com": {"thread": "Tsolo", "subject": "Fwd: New role", "tags": {"inbox"}}}
    )

    outcome = _run_triage(src.meta("solo@dougheadley.com"), src, _new_post_agents(), AsyncMock())

    assert outcome.outcome == "new_created"
    assert "evaluated" in src.messages["solo@dougheadley.com"]["tags"]
    assert "caddy_processed" in src.messages["solo@dougheadley.com"]["tags"]


# ---------------------------------------------------------------------------
# Scenario 3 — forward/original same URL → new_duplicate via dedupe (no 2nd post)
# ---------------------------------------------------------------------------


def test_forward_same_url_as_original_resolves_to_new_duplicate(monkeypatch):
    """When the forward extracts the URL the original already posted, the api
    dedupes (200) → ``new_duplicate`` with exactly ONE create call. This is
    the real double-post guard — NOT thread-tag skipping."""
    monkeypatch.delenv("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", raising=False)
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    monkeypatch.setattr(
        it,
        "extract_job_urls",
        AsyncMock(return_value=SimpleNamespace(job_urls=[_Link(url=JOB_URL)], reasoning="found")),
    )

    # Real _create_posts_from_urls; only the HTTP create is mocked. status 200
    # == api canonical_link dedupe hit (the original already created this post).
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

    src = _ziprecruiter_forward_source()

    outcome = _run_triage(src.meta("fwd@dougheadley.com"), src, _new_post_agents(), AsyncMock())

    assert outcome.outcome == "new_duplicate"
    # Dedupe, not a second JobPost: exactly one create attempt for the one URL.
    assert create_mock.await_count == 1
    # The forward is still marked done so it stops re-matching.
    assert "caddy_processed" in src.messages["fwd@dougheadley.com"]["tags"]
