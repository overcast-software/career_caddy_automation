"""Unit tests for the Mongo observability domain API.

The tests don't reach real Mongo — they monkeypatch ``_db_or_none`` to
return a stand-in object that records the insert / update calls. This
covers the contract surface (which collection got which document) and
the error-tolerance promise (a None db means the API is a no-op).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.observability import triage_store


class _FakeCollection:
    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.updates: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def insert_one(self, doc: dict[str, Any]):
        self.inserts.append(doc)

        class _R:
            inserted_id = "fake-id-1"

        return _R()

    def update_one(self, filt: dict[str, Any], update: dict[str, Any]) -> None:
        self.updates.append((filt, update))


class _FakeDb:
    def __init__(self) -> None:
        self.triage_runs = _FakeCollection()
        self.triage_emails = _FakeCollection()
        self.skipped_duplicates = _FakeCollection()


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(triage_store, "_db_or_none", lambda: db)
    return db


def test_start_run_returns_inserted_id(fake_db):
    run_id = triage_store.start_run("notmuch")
    assert run_id == "fake-id-1"
    assert len(fake_db.triage_runs.inserts) == 1
    doc = fake_db.triage_runs.inserts[0]
    assert doc["backend"] == "notmuch"
    assert doc["finished_at"] is None
    assert doc["started_at"] is not None


def test_start_run_defaults_backend_when_none(fake_db):
    triage_store.start_run(None)
    assert fake_db.triage_runs.inserts[0]["backend"] == "notmuch"


def test_record_email_writes_doc(fake_db):
    triage_store.record_email(
        "run-1",
        "msg-abc@example.com",
        "Welcome aboard",
        "processed",
        ["evaluated", "job_post"],
    )
    assert len(fake_db.triage_emails.inserts) == 1
    doc = fake_db.triage_emails.inserts[0]
    assert doc["run_id"] == "run-1"
    assert doc["email_id"] == "msg-abc@example.com"
    assert doc["outcome"] == "processed"
    assert doc["tags_added"] == ["evaluated", "job_post"]
    assert doc["exception_class"] is None
    assert doc["network_failure"] is False


def test_record_email_persists_introspection_when_present(fake_db):
    """AUTO-33: the stage-E extraction-diagnostic sub-document lands under
    ``introspection`` so the outcome self-explains from a Mongo query."""
    intro = {
        "body_chars": 570,
        "body_url_count": 0,
        "body_nontext_only": True,
        "extract_kept": 0,
        "extract_reasoning": "0 kept — html-only body",
    }
    triage_store.record_email(
        "run-1",
        "msg-html@example.com",
        "Fwd: SWE",
        "new_no_urls",
        ["caddy_processed"],
        introspection=intro,
    )
    doc = fake_db.triage_emails.inserts[0]
    assert doc["introspection"] == intro
    assert doc["introspection"]["body_nontext_only"] is True


def test_record_email_omits_introspection_when_none(fake_db):
    """Emails that exit before extraction carry no introspection — the key is
    absent rather than a null, keeping those docs lean."""
    triage_store.record_email(
        "run-1",
        "msg-early@example.com",
        "Newsletter",
        "not_job",
        ["evaluated"],
    )
    doc = fake_db.triage_emails.inserts[0]
    assert "introspection" not in doc


def test_record_email_carries_exception_metadata(fake_db):
    triage_store.record_email(
        "run-1",
        "msg-2@example.com",
        "subj",
        "network_error",
        [],
        exception_class="ConnectError",
        network_failure=True,
    )
    doc = fake_db.triage_emails.inserts[0]
    assert doc["outcome"] == "network_error"
    assert doc["exception_class"] == "ConnectError"
    assert doc["network_failure"] is True


def test_finish_run_patches_run_doc(fake_db):
    triage_store.finish_run("run-9", total_emails=3, counters={"processed": 2, "not_job": 1})
    assert len(fake_db.triage_runs.updates) == 1
    filt, update = fake_db.triage_runs.updates[0]
    assert filt == {"_id": "run-9"}
    assert update["$set"]["total_emails"] == 3
    assert update["$set"]["counters"] == {"processed": 2, "not_job": 1}
    assert update["$set"]["finished_at"] is not None


def test_finish_run_noop_when_run_id_none(fake_db):
    triage_store.finish_run(None, total_emails=0, counters={})
    assert fake_db.triage_runs.updates == []


def test_record_skipped_duplicate_writes_doc(fake_db):
    triage_store.record_skipped_duplicate(
        "run-1",
        "msg-1@example.com",
        incoming_title="Senior Python Eng",
        incoming_company="Acme",
        incoming_link="https://acme.com/jobs/1",
        matched_post_id=4242,
        confidence=0.91,
        reason="canonical_link match",
        source="email_url",
    )
    assert len(fake_db.skipped_duplicates.inserts) == 1
    doc = fake_db.skipped_duplicates.inserts[0]
    assert doc["matched_post_id"] == 4242
    assert doc["confidence"] == pytest.approx(0.91)
    assert doc["reason"] == "canonical_link match"


def test_db_unreachable_is_silent(monkeypatch, caplog):
    """Mongo outage must not raise — every domain API just logs + returns."""
    monkeypatch.setattr(triage_store, "_db_or_none", lambda: None)
    # No exception, no return value beyond None.
    assert triage_store.start_run("notmuch") is None
    triage_store.record_email(None, "msg", "subj", "already_done", [])
    triage_store.finish_run(None, 0, {})
    triage_store.record_skipped_duplicate(
        None,
        "msg",
        incoming_title=None,
        incoming_company=None,
        incoming_link=None,
        matched_post_id=None,
        confidence=None,
        reason=None,
        source=None,
    )


class TestClassifyException:
    """Buckets cover the three Phase A1 outcomes; class-name heuristic
    catches httpx/openai families without importing those packages."""

    def test_connect_error_is_network(self):
        class ConnectError(Exception):
            pass

        bucket, network = triage_store.classify_exception(ConnectError())
        assert bucket == "network_error"
        assert network is True

    def test_read_timeout_is_network(self):
        class ReadTimeout(Exception):
            pass

        bucket, network = triage_store.classify_exception(ReadTimeout())
        assert bucket == "network_error"
        assert network is True

    def test_api_status_error_is_llm(self):
        class APIStatusError(Exception):
            pass

        bucket, network = triage_store.classify_exception(APIStatusError())
        assert bucket == "llm_error"
        assert network is False

    def test_rate_limit_is_llm(self):
        class RateLimitError(Exception):
            pass

        bucket, network = triage_store.classify_exception(RateLimitError())
        assert bucket == "llm_error"
        assert network is False

    def test_unknown_falls_through(self):
        class SomethingWeird(Exception):
            pass

        bucket, network = triage_store.classify_exception(SomethingWeird())
        assert bucket == "unknown_error"
        assert network is False


def test_db_name_from_uri():
    from src.observability.mongo_client import _db_name_from_uri

    assert _db_name_from_uri("mongodb://localhost:27017/cc_auto") == "cc_auto"
    assert _db_name_from_uri("mongodb://localhost:27017/") == "cc_auto"
    assert _db_name_from_uri("mongodb://localhost:27017") == "cc_auto"
    assert _db_name_from_uri("mongodb://h:27017/other_db") == "other_db"
