"""AUTO-33 — extraction introspection baked into the per-email triage record.

The ``triage_emails`` Mongo doc must self-explain its outcome — especially
``new_no_urls`` — so diagnosing it needs only a Mongo query, not an ad-hoc
script. ``_build_introspection`` captures the exact signals that were
invisible before: how many chars the body loaded to, how many raw URLs it
held, whether it was the html-only ``Non-text part:`` placeholder, and what
the extractor kept + reasoned.

Pins three contracts:

* an html-only body records ``body_nontext_only=True`` / ``body_url_count=0``;
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
# _triage_one stage E — introspection threaded onto the TriageOutcome
# ---------------------------------------------------------------------------


class _Agent:
    """Minimal pydantic-ai Agent stand-in: ``.run()`` returns ``.output``."""

    def __init__(self, output):
        self._output = output

    async def run(self, *args, **kwargs):
        return SimpleNamespace(output=self._output)


def _thin_inline() -> SimpleNamespace:
    """An inline-extract result too thin to post → new_no_urls."""
    return SimpleNamespace(title="", confidence=0.0, evidence="too thin to stand as a post")


def _extract_meta() -> EmailMeta:
    """An already-classified forward (``evaluated``/``job_post``) — skips the
    classify call and routes straight to stage E (extract → create JobPost)."""
    return EmailMeta(
        id="m-extract@example.com",
        subject="Fwd: Principal Software Engineer",
        tags={"evaluated", "job_post"},
        thread_id="t-extract",
    )


def _drive(
    monkeypatch, *, body: str, extracted: SimpleNamespace, inline_output=None
) -> it.TriageOutcome:
    """Run ``_triage_one`` through stage E (and the inline fallback when the
    extractor finds no URLs) with the agents/notmuch/api stubbed.

    The pre-set tags skip the classify call, so ``classify_agent`` can be
    ``None``. The inline agent is exercised only when ``extracted.job_urls``
    is empty.
    """
    monkeypatch.setattr(it, "_load_email_text", lambda email_id: body)
    monkeypatch.setattr(it, "extract_job_urls", AsyncMock(return_value=extracted))
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
    inline_agent = _Agent(inline_output if inline_output is not None else _thin_inline())
    api = MagicMock()
    return asyncio.run(
        it._triage_one(
            _extract_meta(),
            source,
            None,  # classify_agent (skipped — already evaluated)
            inline_agent,
            api,
        )
    )


class TestTriageOneIntrospection:
    def test_html_only_records_nontext_outcome_new_no_urls(self, monkeypatch):
        outcome = _drive(
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
        outcome = _drive(
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
        outcome = _drive(
            monkeypatch,
            body=HTML_ONLY_BODY,
            extracted=_extracted([], "0 kept"),
        )
        assert outcome.outcome == "new_no_urls"
        assert outcome.introspection is None
