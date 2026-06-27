"""Forward-path body rendering — an html-only forward must render to real
text with its links intact, not notmuch's ``Non-text part:`` placeholder.

Thunderbird forwards are frequently ``text/html``-only. The old
``notmuch show --format=text`` path returned the placeholder (≈0 URLs) and
every such forward dead-ended at ``new_no_urls``. ``_load_email_text`` now
pulls the RAW message, extracts its bodies (``extract_bodies``), and renders
html to markdown (``html_to_markdown``) when there is no plain part — so the
URL extractor sees the apply link.

This exercises the real ``extract_bodies`` + ``html_to_markdown`` path
end-to-end; only the ``notmuch show`` subprocess is stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import scripts.inbox_triage as it

# A single-part text/html forward (no text/plain alternative).
HTML_ONLY_RAW = (
    "From: recruiter@acme.com\r\n"
    "To: forwarding@careercaddy.online\r\n"
    "Subject: Fwd: Senior Software Engineer\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    "<html><body><p>We thought you'd be a great fit.</p>"
    '<a href="https://acme.com/jobs/senior-engineer">Apply here</a>'
    "</body></html>\r\n"
)

# A plain-text forward — preferred over html when present.
PLAIN_RAW = (
    "From: jobs@board.example\r\n"
    "To: forwarding@careercaddy.online\r\n"
    "Subject: Fwd: New role\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Apply: https://board.example/jobs/123\r\n"
)


def _stub_notmuch_raw(monkeypatch, raw: str) -> None:
    def _run(argv, **kwargs):
        assert "--format=raw" in argv, f"expected raw show, got {argv}"
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    monkeypatch.setattr(it.subprocess, "run", _run)


def test_load_email_text_renders_html_only_body(monkeypatch):
    """An html-only forward renders to non-empty markdown that preserves the
    apply URL — the previously-starved new_no_urls case."""
    _stub_notmuch_raw(monkeypatch, HTML_ONLY_RAW)

    text = it._load_email_text("msg-html@acme.com")

    assert text.strip(), "html-only body must not render empty"
    assert "Non-text part" not in text
    assert "acme.com/jobs/senior-engineer" in text


def test_load_email_text_prefers_plain_text(monkeypatch):
    """When a plain-text part exists it is returned as-is (no html render)."""
    _stub_notmuch_raw(monkeypatch, PLAIN_RAW)

    text = it._load_email_text("msg-plain@board.example")

    assert "board.example/jobs/123" in text


def test_load_email_text_raises_on_notmuch_failure(monkeypatch):
    def _run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="no such id")

    monkeypatch.setattr(it.subprocess, "run", _run)

    try:
        it._load_email_text("missing@x.com")
    except RuntimeError as exc:
        assert "notmuch show failed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on notmuch failure")
