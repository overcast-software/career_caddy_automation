"""De-dup regression for ``extract_bodies`` on forward-as-attachment.

``Message.walk()`` already recurses into ``message/rfc822`` subparts
(their payload is a list → ``is_multipart()`` is True → walk descends),
so an explicit ``message/rfc822`` recursion branch in ``_walk`` double-walks
the nested original and appends its text/plain + text/html twice. These
tests pin that the nested bodies are captured EXACTLY once.
"""

from __future__ import annotations

from email.message import EmailMessage

from src.email_source.mime import extract_bodies

INNER_PLAIN_MARKER = "INNER-PLAIN-APPLY https://acme.com/jobs/eng"
INNER_HTML_MARKER = "INNER-HTML-APPLY"
INNER_HTML_URL = "https://acme.com/jobs/eng-html"
OUTER_NOTE = "OUTER-NOTE Doug forwarded this for evaluation."


def _forward_as_attachment_raw() -> str:
    """multipart/mixed wrapping (a) an outer text/plain note and
    (b) a message/rfc822 whose body is multipart/alternative
    (text/plain + text/html)."""
    inner = EmailMessage()
    inner["From"] = "recruiter@acme.com"
    inner["To"] = "doug@example.com"
    inner["Subject"] = "Senior Software Engineer"
    inner.set_content(INNER_PLAIN_MARKER)
    inner.add_alternative(
        f'<html><body><p>{INNER_HTML_MARKER}</p><a href="{INNER_HTML_URL}">Apply</a></body></html>',
        subtype="html",
    )

    outer = EmailMessage()
    outer["From"] = "doug@example.com"
    outer["To"] = "forwarding@careercaddy.online"
    outer["Subject"] = "Fwd: Senior Software Engineer"
    outer.set_content(OUTER_NOTE)
    # Passing an EmailMessage routes through the message content-manager,
    # which sets Content-Type: message/rfc822 itself.
    outer.add_attachment(inner)

    return outer.as_string()


def test_forward_as_attachment_bodies_counted_once():
    plain, html = extract_bodies(_forward_as_attachment_raw())

    # Nested original captured exactly once — not double-walked.
    assert plain.count(INNER_PLAIN_MARKER) == 1
    assert html.count(INNER_HTML_MARKER) == 1
    assert html.count(INNER_HTML_URL) == 1

    # The outer forward note is still present.
    assert OUTER_NOTE in plain
