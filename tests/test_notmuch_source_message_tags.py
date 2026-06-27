"""AUTO-32 — notmuch source must be MESSAGE-granular, not thread-granular.

When Doug forwards a ZipRecruiter-style alert, his forward lands in the
*same notmuch thread* as the original alert. The pre-AUTO-32 source
(commit 91a5dd1) judged and tagged at **thread** granularity:

* read side — ``list_pending`` set ``EmailMeta.tags`` from the thread
  union, so an unprocessed forward inherited the original sibling's
  ``evaluated``/``caddy_processed`` and the orchestrator short-circuited it
  to ``already_done`` (never posted, re-matched every cycle).
* write side — ``add_tags`` tagged ``thread:{id}``, stamping a processed
  message's tags onto its not-yet-processed siblings.

These tests pin the fix: both sides operate on the matched MESSAGE.

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import src.email_source.notmuch_source as ns
from src.email_source.notmuch_source import NotmuchSource

# A real ZipRecruiter-forward thread (notmuch search summary), reduced.
# matched=1, total=2: only the forward matches the pending query; the thread
# union tags are poisoned by the already-processed original.
_FWD_ID = "ee8dbf8b-127b-4fc5-b9fe-bc07b9467365@dougheadley.com"
_ORIG_ID = "20260613142458.19da60b58c3aae25@mg.ziprecruiter.com"
_THREAD = {
    "thread": "00000000000155f3",
    "subject": "Fwd: Software Engineer, Frontend opening at Red Hook Interactive LLC",
    "query": [f"id:{_FWD_ID}", f"id:{_ORIG_ID}"],
    # Thread union — poisoned. The forward's OWN tags are just {"inbox"}.
    "tags": ["caddy_processed", "evaluated", "inbox", "job_post", "passed", "refined"],
}


def _route_run(summary_threads, msg_tags):
    """Build a subprocess.run stand-in that routes notmuch invocations.

    * ``notmuch search --format=json ...``  → the thread summary list
    * ``notmuch search --output=tags id:X`` → that message's OWN tags
    """

    def _run(argv, **kwargs):
        if "--format=json" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps(summary_threads), stderr="")
        if "--output=tags" in argv:
            qid = argv[-1]
            assert qid.startswith("id:"), f"expected id: query, got {qid!r}"
            mid = qid[3:]
            body = "\n".join(msg_tags.get(mid, [])) + "\n"
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        raise AssertionError(f"unexpected notmuch argv {argv}")

    return _run


# ---------------------------------------------------------------------------
# read side — list_pending / _message_tags / _matched_message_id
# ---------------------------------------------------------------------------


def test_list_pending_uses_matched_message_own_tags(monkeypatch):
    """The forward's EmailMeta.tags is its OWN tag set ({"inbox"}), NOT the
    poisoned thread union — so the orchestrator won't read it as done."""
    monkeypatch.setattr(
        ns.subprocess, "run", _route_run([_THREAD], {_FWD_ID: ["inbox"], _ORIG_ID: ["evaluated"]})
    )

    metas = asyncio.run(NotmuchSource().list_pending())

    assert len(metas) == 1
    m = metas[0]
    # Routed to the matched forward, thread kept for content-load context.
    assert m.id == _FWD_ID
    assert m.thread_id == "00000000000155f3"
    # The fix: own tags, not the thread union.
    assert m.tags == {"inbox"}
    assert "evaluated" not in m.tags
    assert "caddy_processed" not in m.tags


def test_list_by_query_also_message_granular(monkeypatch):
    """--show <state> shares the same row builder, so it too reports own tags."""
    monkeypatch.setattr(
        ns.subprocess, "run", _route_run([_THREAD], {_FWD_ID: ["inbox", "job_post"]})
    )

    metas = asyncio.run(NotmuchSource().list_by_query("tag:job_post"))

    assert len(metas) == 1
    assert metas[0].id == _FWD_ID
    assert metas[0].tags == {"inbox", "job_post"}


def test_message_tags_returns_own_tag_set(monkeypatch):
    monkeypatch.setattr(
        ns.subprocess,
        "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="inbox\njob_post\n", stderr=""),
    )
    assert ns._message_tags(_FWD_ID) == {"inbox", "job_post"}


def test_matched_message_id_takes_first_of_multi_match():
    thread = {"query": [f"id:{_FWD_ID} id:other@x.com", None]}
    assert ns._matched_message_id(thread) == _FWD_ID


def test_matched_message_id_none_when_no_match():
    assert ns._matched_message_id({"query": [None, "id:x"]}) is None
    assert ns._matched_message_id({"query": []}) is None


# ---------------------------------------------------------------------------
# write side — add_tags targets the message, never the thread
# ---------------------------------------------------------------------------


def test_add_tags_targets_message_not_thread(monkeypatch):
    captured: dict = {}

    def _run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ns.subprocess, "run", _run)

    asyncio.run(NotmuchSource().add_tags(_FWD_ID, ["evaluated", "job_post"]))

    argv = captured["argv"]
    assert argv[:2] == ["notmuch", "tag"]
    assert "+evaluated" in argv and "+job_post" in argv
    # The crux: tag the MESSAGE; thread siblings must stay untouched.
    assert f"id:{_FWD_ID}" in argv
    assert not any(isinstance(a, str) and a.startswith("thread:") for a in argv)


def test_add_tags_noop_on_empty(monkeypatch):
    calls = {"n": 0}

    def _run(argv, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ns.subprocess, "run", _run)

    asyncio.run(NotmuchSource().add_tags(_FWD_ID, []))

    assert calls["n"] == 0
