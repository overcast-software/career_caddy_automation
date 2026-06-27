"""AUTO-33 — extraction introspection baked into the per-email triage record.

The ``triage_emails`` Mongo doc must self-explain its outcome — especially
``new_no_urls`` — so diagnosing it needs only a Mongo query, not an ad-hoc
script. ``_build_introspection`` captures the exact signals that were
invisible before: how many chars the body loaded to, how many raw URLs it
held, whether it was the html-only ``Non-text part:`` placeholder, and what
the stage-5 extractor kept + reasoned.

Pins three contracts:

* a stage-5 html-only body records ``body_nontext_only=True`` /
  ``body_url_count=0``;
* a multipart body records ``body_url_count>0`` + a non-empty
  ``extract_reasoning``;
* a raised exception while *building* the introspection yields ``None`` and
  never changes the email's real outcome (observability is fail-safe).

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import scripts.inbox_triage as it
from src.email_source import EmailMeta

# An html-only Thunderbird forward: notmuch emits the placeholder instead of a
# body, so the extractor sees ~no text and zero URLs -> new_no_urls.
HTML_ONLY_BODY = (
    "Subject: Fwd: Principal Software Engineer\n"
    "From: doug@example.com\n\n"
    "Non-text part: text/html\n"
)

# A multipart original: real text with several apply/tracker URLs.
MULTIPART_BODY = (
    "Subject: New roles for you\n\n"
    "Apply here: https://lever.co/acme/abc123 or "
    "https://greenhouse.io/co/jobs/42 — reply to mailto:hr@acme.com\n"
)


def _extracted(job_urls: list, reasoning: str) -> SimpleNamespace:
    """Stand-in for ``ExtractedUrls`` — ``_build_introspection`` only reads
    ``.job_urls`` (length) and ``.reasoning``."""
    return SimpleNamespace(job_urls=job_urls, reasoning=reasoning)


# ---------------------------------------------------------------------------
# _build_introspection — the helper in isolation
# ---------------------------------------------------------------------------


class TestBuildIntrospection:
    def test_html_only_body_is_nontext_with_zero_urls(self):
        extracted = _extracted([], "0 kept, 0 dropped — html-only body")
        intro = it._build_introspection(HTML_ONLY_BODY, extracted)
        assert intro is not None
        assert intro["body_nontext_only"] is True
        assert intro["body_url_count"] == 0
        assert intro["body_chars"] == len(HTML_ONLY_BODY)
        assert intro["extract_kept"] == 0
        assert intro["extract_reasoning"] == "0 kept, 0 dropped — html-only body"

    def test_multipart_body_counts_urls_and_keeps_reasoning(self):
        extracted = _extracted(
            [SimpleNamespace(url="https://lever.co/acme/abc123")],
            "1 kept, 2 dropped (tracking)",
        )
        intro = it._build_introspection(MULTIPART_BODY, extracted)
        assert intro is not None
        # Two https + one mailto in the body text.
        assert intro["body_url_count"] == 3
        assert intro["body_nontext_only"] is False
        assert intro["extract_kept"] == 1
        assert intro["extract_reasoning"]  # non-empty
        assert intro["extract_reasoning"] == "1 kept, 2 dropped (tracking)"

    def test_extracted_none_omits_extract_fields(self):
        intro = it._build_introspection("https://acme.com/jobs/1", None)
        assert intro is not None
        assert intro["body_url_count"] == 1
        assert intro["body_nontext_only"] is False
        assert "extract_kept" not in intro
        assert "extract_reasoning" not in intro

    def test_build_failure_returns_none(self, monkeypatch):
        # Force the URL count to blow up; the helper must swallow it and return
        # None rather than propagate (observability is never load-bearing).
        monkeypatch.setattr(it, "_count_body_urls", MagicMock(side_effect=RuntimeError("boom")))
        assert it._build_introspection(MULTIPART_BODY, _extracted([], "x")) is None


# ---------------------------------------------------------------------------
# _triage_one stage 5 — introspection is threaded onto the TriageOutcome
# ---------------------------------------------------------------------------


def _stage5_meta() -> EmailMeta:
    """An email already tagged through refine with no follow_up/inline_post —
    routes straight to stage 5 (URL-extract → create JobPost)."""
    return EmailMeta(
        id="m-stage5@example.com",
        subject="Fwd: Principal Software Engineer",
        tags={"evaluated", "job_post", "refined"},
        thread_id="t-stage5",
    )


def _drive_stage5(monkeypatch, *, body: str, extracted: SimpleNamespace) -> it.TriageOutcome:
    """Run ``_triage_one`` through stage 5 with the agents/notmuch/api stubbed.

    Stages 1-4 are skipped by the pre-set tags, so the classify/refine/followup
    agents are never called and can be ``None``.
    """
    monkeypatch.setattr(it, "_load_email_text", lambda email_id: body)
    monkeypatch.setattr(it, "extract_job_urls", AsyncMock(return_value=extracted))
    # If the extractor kept URLs, stage 5 calls _create_posts_from_urls — stub
    # it so no real api traffic happens; report a clean create.
    monkeypatch.setattr(
        it,
        "_create_posts_from_urls",
        AsyncMock(
            return_value={
                "created": [link.url for link in extracted.job_urls],
                "duplicates": [],
                "failed": [],
                "scrapes_queued": 0,
            }
        ),
    )
    source = MagicMock()
    source.add_tags = AsyncMock()
    api = MagicMock()
    deps = MagicMock()
    return asyncio.run(
        it._triage_one(
            _stage5_meta(),
            source,
            None,  # classify_agent
            None,  # refine_agent
            None,  # followup_agent
            None,  # inline_post_agent
            api,
            deps,
        )
    )


class TestTriageOneIntrospection:
    def test_html_only_records_nontext_outcome_new_no_urls(self, monkeypatch):
        outcome = _drive_stage5(
            monkeypatch,
            body=HTML_ONLY_BODY,
            extracted=_extracted([], "0 kept — body was html-only"),
        )
        assert outcome.outcome == "new_no_urls"
        assert outcome.introspection is not None
        assert outcome.introspection["body_nontext_only"] is True
        assert outcome.introspection["body_url_count"] == 0
        assert outcome.introspection["extract_kept"] == 0

    def test_multipart_records_url_count_and_reasoning(self, monkeypatch):
        outcome = _drive_stage5(
            monkeypatch,
            body=MULTIPART_BODY,
            extracted=_extracted(
                [SimpleNamespace(url="https://lever.co/acme/abc123")],
                "1 kept, 2 dropped",
            ),
        )
        assert outcome.outcome == "new_created"
        assert outcome.introspection is not None
        assert outcome.introspection["body_url_count"] > 0
        assert outcome.introspection["extract_reasoning"] == "1 kept, 2 dropped"

    def test_introspection_failure_does_not_change_outcome(self, monkeypatch):
        # An exception while *building* introspection must leave the real
        # outcome intact and the field None.
        monkeypatch.setattr(it, "_count_body_urls", MagicMock(side_effect=RuntimeError("boom")))
        outcome = _drive_stage5(
            monkeypatch,
            body=HTML_ONLY_BODY,
            extracted=_extracted([], "0 kept"),
        )
        assert outcome.outcome == "new_no_urls"
        assert outcome.introspection is None
