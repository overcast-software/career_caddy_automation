"""Unit tests for :func:`parse_catchall_message` — the only IMAP
surface the B3 catchall poller needs to be confident in beyond the
mocked network calls.

Recipient resolution covers the three header families the catchall
poller cares about (Delivered-To, X-Original-To, To) plus a couple of
edge cases (Bcc-only delivery via Delivered-To, off-domain recipients
ignored, malformed addresses skipped).
"""

from __future__ import annotations

from email.message import EmailMessage

from src.email_source.imap_source import parse_catchall_message


def _build_raw(
    *,
    to: str = "",
    delivered_to: str = "",
    x_original_to: str = "",
    cc: str = "",
    subject: str = "fwd: job",
    body_plain: str = "https://acme.com/jobs/1",
    body_html: str | None = None,
    sender: str = "user@gmail.com",
    message_id: str = "<m-1@catchall>",
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Message-Id"] = message_id
    if to:
        msg["To"] = to
    if delivered_to:
        msg["Delivered-To"] = delivered_to
    if x_original_to:
        msg["X-Original-To"] = x_original_to
    if cc:
        msg["Cc"] = cc
    if body_html is not None:
        msg.set_content(body_plain)
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_plain)
    return msg.as_bytes()


def test_to_header_only_resolves_to_catchall_localpart():
    raw = _build_raw(to="dough@careercaddy.online")
    parsed = parse_catchall_message(raw, uid="1", catchall_domain="careercaddy.online")
    assert parsed.forwarded_to_localpart == "dough"
    assert parsed.forwarded_via_address == "dough@careercaddy.online"
    assert parsed.message_id == "<m-1@catchall>"


def test_delivered_to_wins_over_to_when_user_bcc_forwards():
    """Some users Bcc their catchall address — To: holds the original
    recipient, Delivered-To: holds where the message actually landed.
    The poller should prefer Delivered-To."""
    raw = _build_raw(
        to="someone-else@example.com",
        delivered_to="dough@careercaddy.online",
    )
    parsed = parse_catchall_message(raw, uid="2", catchall_domain="careercaddy.online")
    assert parsed.forwarded_to_localpart == "dough"


def test_x_original_to_resolves_when_only_header():
    raw = _build_raw(
        to="someone-else@example.com",
        x_original_to="alice@careercaddy.online",
    )
    parsed = parse_catchall_message(raw, uid="3", catchall_domain="careercaddy.online")
    assert parsed.forwarded_to_localpart == "alice"


def test_off_domain_recipient_resolves_to_none():
    """No address on the catchall domain → forwarded_to_localpart None.
    The poller routes these to parse_failed."""
    raw = _build_raw(to="elsewhere@example.com")
    parsed = parse_catchall_message(raw, uid="4", catchall_domain="careercaddy.online")
    assert parsed.forwarded_to_localpart is None
    assert parsed.forwarded_via_address is None


def test_multiple_recipients_first_catchall_match_wins():
    raw = _build_raw(
        to="elsewhere@example.com",
        cc="dough@careercaddy.online, alice@careercaddy.online",
    )
    parsed = parse_catchall_message(raw, uid="5", catchall_domain="careercaddy.online")
    # Cc is iterated in header-order after Delivered-To/X-Original-To/To,
    # so the dough address is the first catchall-domain match.
    assert parsed.forwarded_to_localpart == "dough"


def test_case_folding_on_domain_and_localpart():
    raw = _build_raw(to="Dough@CareerCaddy.Online")
    parsed = parse_catchall_message(raw, uid="6", catchall_domain="careercaddy.online")
    # Localpart is case-folded (the api's catchall validator is
    # case-insensitive per PR #151).
    assert parsed.forwarded_to_localpart == "dough"


def test_body_text_returned_from_plain_part():
    raw = _build_raw(body_plain="https://acme.com/jobs/1 — Senior Backend Engineer")
    parsed = parse_catchall_message(raw, uid="7", catchall_domain="careercaddy.online")
    assert "acme.com/jobs/1" in parsed.body_text


def test_html_only_falls_back_to_html_body():
    raw = _build_raw(
        body_plain="",
        body_html="<p>Apply at <a href='https://acme.com/jobs/2'>this role</a></p>",
    )
    parsed = parse_catchall_message(raw, uid="8", catchall_domain="careercaddy.online")
    # Either source is fine — we just need the URL to be reachable to
    # the downstream LLM extractor.
    assert "acme.com/jobs/2" in parsed.body_text
