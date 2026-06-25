"""AUTO-20 — folder scope + per-message recipient surfacing.

Two units, both exercised without a live notmuch index:

* the pure query builder ``_scoped_query`` (default/configured folder emits
  ``folder:"…"``; explicitly-empty env reproduces the legacy un-scoped query),
  and the folder scope reaching all three query paths (``list_pending`` /
  ``count_by_query`` / ``list_by_query``);
* ``_recipient_from_message`` / ``get_recipient`` header preference — the
  envelope recipient (Delivered-To) beats the original ``To:``.
"""

from __future__ import annotations

import asyncio
import types

from src.email_source import (
    _DEFAULT_INBOX_NOTMUCH_FOLDER,
    notmuch_folder_scope,
)
from src.email_source.notmuch_source import (
    _PENDING_QUERY,
    NotmuchSource,
    _recipient_from_message,
    _scoped_query,
)

_FOLDER = "forwarding@careercaddy.online"


# --------------------------------------------------------------------------- #
# env resolution — CADDY_INBOX_NOTMUCH_FOLDER                                  #
# --------------------------------------------------------------------------- #
class TestFolderScopeEnv:
    def test_unset_defaults_to_catchall(self, monkeypatch):
        monkeypatch.delenv("CADDY_INBOX_NOTMUCH_FOLDER", raising=False)
        assert notmuch_folder_scope() == _DEFAULT_INBOX_NOTMUCH_FOLDER
        assert _DEFAULT_INBOX_NOTMUCH_FOLDER == _FOLDER

    def test_configured_value_is_used_verbatim(self, monkeypatch):
        monkeypatch.setenv("CADDY_INBOX_NOTMUCH_FOLDER", "Jobs/recruiters")
        assert notmuch_folder_scope() == "Jobs/recruiters"

    def test_explicitly_empty_is_legacy_whole_index(self, monkeypatch):
        # The OSS / un-pre-filtered escape hatch: empty env disables scoping.
        monkeypatch.setenv("CADDY_INBOX_NOTMUCH_FOLDER", "")
        assert notmuch_folder_scope() is None

    def test_whitespace_only_is_legacy_whole_index(self, monkeypatch):
        monkeypatch.setenv("CADDY_INBOX_NOTMUCH_FOLDER", "   ")
        assert notmuch_folder_scope() is None


# --------------------------------------------------------------------------- #
# pure query builder                                                           #
# --------------------------------------------------------------------------- #
class TestScopedQuery:
    def test_default_folder_emits_quoted_folder_token(self):
        q = _scoped_query(_PENDING_QUERY, 14, _FOLDER)
        assert f'folder:"{_FOLDER}"' in q
        # base query and the date window survive alongside the scope.
        assert _PENDING_QUERY in q
        assert "date:" in q

    def test_configured_folder_emits_its_token(self):
        q = _scoped_query("tag:job_post", 7, "Jobs/recruiters")
        assert 'folder:"Jobs/recruiters"' in q
        assert "tag:job_post" in q

    def test_none_folder_is_legacy_unscoped_query(self):
        q = _scoped_query(_PENDING_QUERY, 14, None)
        assert "folder:" not in q
        # Byte-identical to the pre-AUTO-20 date-scoped form.
        assert q.startswith(f"({_PENDING_QUERY}) AND date:")


# --------------------------------------------------------------------------- #
# folder scope reaches all three subprocess query paths                        #
# --------------------------------------------------------------------------- #
class _RecordingRun:
    """Stand-in for ``subprocess.run`` that records the argv of each call and
    returns empty-but-valid notmuch output for search/count."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        stdout = "0" if "count" in argv else "[]"
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    @property
    def queries(self) -> list[str]:
        # In every path the assembled query string is the final argv element.
        return [call[-1] for call in self.calls]


class TestFolderScopeInAllQueryPaths:
    def _run(self, monkeypatch) -> _RecordingRun:
        rec = _RecordingRun()
        monkeypatch.setattr("src.email_source.notmuch_source.subprocess.run", rec)
        return rec

    def test_list_pending_scopes_to_folder(self, monkeypatch):
        rec = self._run(monkeypatch)
        src = NotmuchSource(folder=_FOLDER)
        asyncio.run(src.list_pending())
        assert rec.queries and all(f'folder:"{_FOLDER}"' in q for q in rec.queries)

    def test_count_by_query_scopes_to_folder(self, monkeypatch):
        rec = self._run(monkeypatch)
        src = NotmuchSource(folder=_FOLDER)
        asyncio.run(src.count_by_query("tag:job_post AND tag:refined"))
        assert rec.queries and all(f'folder:"{_FOLDER}"' in q for q in rec.queries)

    def test_list_by_query_scopes_to_folder(self, monkeypatch):
        rec = self._run(monkeypatch)
        src = NotmuchSource(folder=_FOLDER)
        asyncio.run(src.list_by_query("tag:follow_up"))
        assert rec.queries and all(f'folder:"{_FOLDER}"' in q for q in rec.queries)

    def test_legacy_none_folder_omits_scope_in_all_paths(self, monkeypatch):
        rec = self._run(monkeypatch)
        src = NotmuchSource(folder=None)
        asyncio.run(src.list_pending())
        asyncio.run(src.count_by_query("tag:job_post"))
        asyncio.run(src.list_by_query("tag:follow_up"))
        assert rec.queries and all("folder:" not in q for q in rec.queries)


# --------------------------------------------------------------------------- #
# recipient header preference                                                  #
# --------------------------------------------------------------------------- #
_RAW_WITH_DELIVERED_TO = (
    "Delivered-To: alice@careercaddy.online\r\n"
    "X-Original-To: alice@careercaddy.online\r\n"
    "To: jobs@linkedin.com\r\n"
    "From: recruiter@example.com\r\n"
    "Subject: A role you might like\r\n"
    "\r\n"
    "Body text.\r\n"
)


class TestRecipientFromMessage:
    def test_delivered_to_beats_to(self):
        assert _recipient_from_message(_RAW_WITH_DELIVERED_TO) == "alice@careercaddy.online"

    def test_x_original_to_used_when_no_delivered_to(self):
        raw = "X-Original-To: bob@careercaddy.online\r\nTo: jobs@indeed.com\r\n\r\nbody\r\n"
        assert _recipient_from_message(raw) == "bob@careercaddy.online"

    def test_envelope_to_used_before_to(self):
        raw = "Envelope-To: carol@careercaddy.online\r\nTo: jobs@indeed.com\r\n\r\nbody\r\n"
        assert _recipient_from_message(raw) == "carol@careercaddy.online"

    def test_to_is_the_fallback(self):
        raw = "To: dave@careercaddy.online\r\nFrom: x@y.com\r\n\r\nbody\r\n"
        assert _recipient_from_message(raw) == "dave@careercaddy.online"

    def test_first_delivered_to_when_multiple_hops(self):
        raw = (
            "Delivered-To: erin@careercaddy.online\r\n"
            "Delivered-To: relay@example.net\r\n"
            "To: jobs@indeed.com\r\n"
            "\r\nbody\r\n"
        )
        assert _recipient_from_message(raw) == "erin@careercaddy.online"

    def test_none_when_no_recipient_headers(self):
        raw = "From: x@y.com\r\nSubject: no recipient\r\n\r\nbody\r\n"
        assert _recipient_from_message(raw) is None


class TestGetRecipient:
    def test_reads_raw_message_and_prefers_envelope(self, monkeypatch):
        def fake_run(argv, **kwargs):
            assert argv[:3] == ["notmuch", "show", "--format=raw"]
            return types.SimpleNamespace(returncode=0, stdout=_RAW_WITH_DELIVERED_TO, stderr="")

        monkeypatch.setattr("src.email_source.notmuch_source.subprocess.run", fake_run)
        src = NotmuchSource(folder=_FOLDER)
        assert asyncio.run(src.get_recipient("msg-123@x")) == "alice@careercaddy.online"

    def test_returns_none_on_notmuch_failure(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="not found")

        monkeypatch.setattr("src.email_source.notmuch_source.subprocess.run", fake_run)
        src = NotmuchSource(folder=_FOLDER)
        assert asyncio.run(src.get_recipient("missing@x")) is None
