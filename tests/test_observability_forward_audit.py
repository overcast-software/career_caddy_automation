"""Unit tests for the forward_audit observability surface.

Same pattern as :mod:`test_observability_triage` — monkeypatch
``_db_or_none`` to a stand-in object that records what gets inserted.
We never touch real Mongo.
"""

from __future__ import annotations

from typing import Any

from src.observability import forward_audit


class _FakeCollection:
    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self._count_return = 0

    def insert_one(self, doc: dict[str, Any]):
        self.inserts.append(doc)

        class _R:
            inserted_id = "fake-id-1"

        return _R()

    def count_documents(self, filt: dict[str, Any]) -> int:
        self.count_calls.append(filt)
        return self._count_return


class _FakeDb:
    def __init__(self) -> None:
        self.forward_audit = _FakeCollection()


def _patched_db(monkeypatch) -> _FakeDb:
    db = _FakeDb()
    monkeypatch.setattr(forward_audit, "_db_or_none", lambda: db)
    return db


def test_record_forward_audit_writes_doc(monkeypatch):
    db = _patched_db(monkeypatch)
    forward_audit.record_forward_audit(
        email_id="<msg-1@catchall>",
        forwarded_to_localpart="dough",
        forwarded_via_address="dough@careercaddy.online",
        resolved_user_id=2,
        outcome="created",
        job_post_id=4242,
        quota_remaining=99,
        subject="Senior Backend Engineer @ Acme",
        sender="recruiter@acme.com",
    )
    assert len(db.forward_audit.inserts) == 1
    doc = db.forward_audit.inserts[0]
    assert doc["email_id"] == "<msg-1@catchall>"
    assert doc["forwarded_to_localpart"] == "dough"
    assert doc["forwarded_via_address"] == "dough@careercaddy.online"
    assert doc["resolved_user_id"] == 2
    assert doc["outcome"] == "created"
    # job_post_id is stringified so Mongo doesn't get mixed int/str docs.
    assert doc["job_post_id"] == "4242"
    assert doc["quota_remaining"] == 99
    assert doc["recorded_at"] is not None


def test_record_forward_audit_unknown_outcome_still_stored(monkeypatch, caplog):
    db = _patched_db(monkeypatch)
    forward_audit.record_forward_audit(
        email_id="x",
        forwarded_to_localpart="x",
        forwarded_via_address="x@careercaddy.online",
        resolved_user_id=None,
        outcome="surprise_new_bucket",
    )
    # Stored anyway — the runtime warning must not crash the poller.
    assert db.forward_audit.inserts
    assert db.forward_audit.inserts[0]["outcome"] == "surprise_new_bucket"


def test_record_forward_audit_silent_on_db_outage(monkeypatch):
    monkeypatch.setattr(forward_audit, "_db_or_none", lambda: None)
    # Should not raise.
    forward_audit.record_forward_audit(
        email_id="x",
        forwarded_to_localpart="x",
        forwarded_via_address=None,
        resolved_user_id=None,
        outcome="parse_failed",
    )


def test_record_forward_audit_extras_merged(monkeypatch):
    db = _patched_db(monkeypatch)
    forward_audit.record_forward_audit(
        email_id="x",
        forwarded_to_localpart="dough",
        forwarded_via_address="dough@careercaddy.online",
        resolved_user_id=2,
        outcome="created",
        extras={"uid": "17", "counts": {"created": 1}},
    )
    doc = db.forward_audit.inserts[0]
    assert doc["uid"] == "17"
    assert doc["counts"] == {"created": 1}


def test_count_forwards_today_filters_user_and_window(monkeypatch):
    db = _patched_db(monkeypatch)
    db.forward_audit._count_return = 7
    n = forward_audit.count_forwards_today(2)
    assert n == 7
    assert len(db.forward_audit.count_calls) == 1
    filt = db.forward_audit.count_calls[0]
    assert filt["resolved_user_id"] == 2
    assert "recorded_at" in filt
    assert "$gte" in filt["recorded_at"]


def test_count_forwards_today_zero_on_db_outage(monkeypatch):
    monkeypatch.setattr(forward_audit, "_db_or_none", lambda: None)
    # Fail-open: missing observability returns 0 so the poller still
    # processes mail rather than gate-blocking on a Mongo blip.
    assert forward_audit.count_forwards_today(2) == 0


def test_forward_outcomes_set_is_frozen():
    # The dashboard surface depends on this — any rename of a bucket
    # is a coordinated change with the Metabase questions, not a
    # silent string flip.
    assert isinstance(forward_audit.FORWARD_OUTCOMES, frozenset)
    assert "created" in forward_audit.FORWARD_OUTCOMES
    assert "deduped" in forward_audit.FORWARD_OUTCOMES
    assert "unknown_localpart" in forward_audit.FORWARD_OUTCOMES
    assert "over_quota" in forward_audit.FORWARD_OUTCOMES
    assert "no_urls_extracted" in forward_audit.FORWARD_OUTCOMES
    assert "post_failed" in forward_audit.FORWARD_OUTCOMES
    assert "parse_failed" in forward_audit.FORWARD_OUTCOMES
