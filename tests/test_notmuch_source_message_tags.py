"""AUTO-32 message-granularity + AUTO-18 M1 catchall path: selector.

When Doug forwards a ZipRecruiter-style alert, his forward lands in the
*same notmuch thread* as the original alert. The pre-AUTO-32 source judged
and tagged at **thread** granularity:

* read side — ``list_pending`` set ``EmailMeta.tags`` from the thread
  union, so an unprocessed forward inherited the original sibling's
  ``evaluated``/``caddy_processed`` and the orchestrator short-circuited it
  to ``already_done`` (never posted, re-matched every cycle).
* write side — ``add_tags`` tagged ``thread:{id}``, stamping a processed
  message's tags onto its not-yet-processed siblings.

These tests pin the fix (both sides operate on the matched MESSAGE), the
AUTO-18 M1 catchall ``_PENDING_QUERY`` (``path:<folder>/** AND NOT
tag:caddy_processed`` — the old ``to:"forwarding@…"`` selector starved the
per-user catchall), and that ``EmailMeta.recipient`` is surfaced from the
matched message's headers.

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


def _route_run(summary_threads, msg_tags, msg_raw=None):
    """Build a subprocess.run stand-in that routes notmuch invocations.

    * ``notmuch search --format=json ...``  → the thread summary list
    * ``notmuch search --output=tags id:X`` → that message's OWN tags
    * ``notmuch show --format=raw id:X``     → that message's raw headers/body
      (drives ``EmailMeta.recipient`` via ``extract_recipient``)
    """
    msg_raw = msg_raw or {}

    def _run(argv, **kwargs):
        if "--format=json" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps(summary_threads), stderr="")
        if "--output=tags" in argv:
            qid = argv[-1]
            assert qid.startswith("id:"), f"expected id: query, got {qid!r}"
            mid = qid[3:]
            body = "\n".join(msg_tags.get(mid, [])) + "\n"
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        if "--format=raw" in argv:
            qid = argv[-1]
            assert qid.startswith("id:"), f"expected id: query, got {qid!r}"
            mid = qid[3:]
            return SimpleNamespace(returncode=0, stdout=msg_raw.get(mid, ""), stderr="")
        raise AssertionError(f"unexpected notmuch argv {argv}")

    return _run


# ---------------------------------------------------------------------------
# selector — AUTO-18 M1 catchall path: _PENDING_QUERY
# ---------------------------------------------------------------------------


def test_pending_query_is_path_scoped():
    """The selector sweeps the whole catchall maildir folder by ``path:`` and
    excludes already-processed messages — NOT a ``to:`` recipient match (the
    old form starved the per-user catchall)."""
    assert ns._PENDING_QUERY == f"path:{ns._CATCHALL_FOLDER}/** AND NOT tag:caddy_processed"
    assert ns._PENDING_QUERY.startswith("path:")
    assert "NOT tag:caddy_processed" in ns._PENDING_QUERY
    assert "to:" not in ns._PENDING_QUERY


def test_list_pending_query_is_path_scoped(monkeypatch):
    """list_pending shells the ``path:`` catchall selector, date-bounded —
    never a ``to:`` recipient scope (validated live: ``to:`` starves the
    per-user catchall while ``path:`` captures the whole folder)."""
    captured: dict = {}

    def _run(argv, **kwargs):
        if "--format=json" in argv:
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return SimpleNamespace(returncode=0, stdout="\n", stderr="")

    monkeypatch.setattr(ns.subprocess, "run", _run)
    asyncio.run(NotmuchSource().list_pending())

    query = captured["argv"][-1]
    assert f"path:{ns._CATCHALL_FOLDER}/**" in query
    assert "NOT tag:caddy_processed" in query
    assert "date:" in query
    assert "to:" not in query
    # The path: term must ride in ONE argv element (no shell), so the @ and **
    # glob reach notmuch literally rather than being split or shell-expanded.
    assert captured["argv"][-1] == query and query.count("path:") == 1


# ---------------------------------------------------------------------------
# read side — list_pending / _message_tags / _matched_message_id
# ---------------------------------------------------------------------------


def test_list_pending_uses_matched_message_own_tags(monkeypatch):
    """The forward's EmailMeta.tags is its OWN tag set ({"inbox"}), NOT the
    poisoned thread union — so the orchestrator won't read it as done. Its
    ``recipient`` is surfaced from the matched message's own headers."""
    monkeypatch.setattr(
        ns.subprocess,
        "run",
        _route_run(
            [_THREAD],
            {_FWD_ID: ["inbox"], _ORIG_ID: ["evaluated"]},
            msg_raw={_FWD_ID: "To: dough@careercaddy.online\r\n\r\nbody"},
        ),
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
    # AUTO-18 M1: the @careercaddy.online localpart is surfaced for the owner gate.
    assert m.recipient == "dough"


def test_recipient_none_when_no_caddy_address(monkeypatch):
    """An over-captured personal-alias original (no @careercaddy.online
    recipient) surfaces recipient=None — the triage owner gate drops it."""
    monkeypatch.setattr(
        ns.subprocess,
        "run",
        _route_run(
            [_THREAD],
            {_FWD_ID: ["inbox"]},
            msg_raw={_FWD_ID: "To: doug@passiveobserver.com\r\n\r\nbody"},
        ),
    )

    metas = asyncio.run(NotmuchSource().list_pending())

    assert len(metas) == 1
    assert metas[0].recipient is None


def test_list_by_query_also_message_granular(monkeypatch):
    """--show <state> shares the same row builder, so it too reports own tags."""
    monkeypatch.setattr(
        ns.subprocess, "run", _route_run([_THREAD], {_FWD_ID: ["inbox", "job_post"]})
    )

    metas = asyncio.run(NotmuchSource().list_by_query("tag:job_post"))

    assert len(metas) == 1
    assert metas[0].id == _FWD_ID
    assert metas[0].tags == {"inbox", "job_post"}


def test_list_by_message_id_not_date_scoped(monkeypatch):
    """``--message-id`` targets exactly one message by id with NO ``date:``
    window (a Message-ID is globally unique; date-scoping would only risk
    missing an older message). Returns the matched message's own meta."""
    captured: dict = {}

    def _run(argv, **kwargs):
        if "--format=json" in argv:
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps([_THREAD]), stderr="")
        if "--output=tags" in argv:
            return SimpleNamespace(returncode=0, stdout="inbox\n", stderr="")
        if "--format=raw" in argv:
            return SimpleNamespace(
                returncode=0, stdout="To: dough@careercaddy.online\r\n\r\nbody", stderr=""
            )
        raise AssertionError(f"unexpected notmuch argv {argv}")

    monkeypatch.setattr(ns.subprocess, "run", _run)
    metas = asyncio.run(NotmuchSource().list_by_message_id(_FWD_ID))

    query = captured["argv"][-1]
    assert query == f"id:{_FWD_ID}"  # exact id query, no date wrap
    assert "date:" not in query
    assert "--limit=1" in captured["argv"]
    assert len(metas) == 1 and metas[0].id == _FWD_ID


def test_list_by_message_id_strips_prefix_and_brackets(monkeypatch):
    """Accepts a bare id, an ``id:``-prefixed id, or an ``<angle-bracketed>``
    id — all normalize to the same ``id:<bare>`` query."""
    captured: dict = {}

    def _run(argv, **kwargs):
        if "--format=json" in argv:
            captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(ns.subprocess, "run", _run)
    asyncio.run(NotmuchSource().list_by_message_id(f"id:<{_FWD_ID}>"))
    assert captured["argv"][-1] == f"id:{_FWD_ID}"


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
