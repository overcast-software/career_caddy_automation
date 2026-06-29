"""AUTO-18 M1 — ``extract_recipient`` derives the catchall owner localpart.

The per-user catchall delivers every ``<username>@careercaddy.online`` message
into one maildir; the triage owner gate keys on the localpart this message was
addressed to. ``extract_recipient`` scans the recipient headers in priority
``Delivered-To`` > ``X-Original-To`` > ``To`` and returns the localpart of the
first ``@careercaddy.online`` address, or ``None`` when none is present (an
over-captured personal-alias original — the gate drops it without a JobPost).

No pytest-asyncio needed — ``extract_recipient`` is a pure function.
"""

from __future__ import annotations

from src.email_source.mime import extract_recipient


def test_to_header_localpart():
    raw = "From: jobs@board.example\r\nTo: dough@careercaddy.online\r\n\r\nbody\r\n"
    assert extract_recipient(raw) == "dough"


def test_delivered_to_wins_over_to():
    """Delivered-To reflects the actual envelope drop and outranks a cosmetic
    To when both are present."""
    raw = (
        "From: jobs@board.example\r\n"
        "Delivered-To: wisevehicle@careercaddy.online\r\n"
        "To: dough@careercaddy.online\r\n"
        "\r\nbody\r\n"
    )
    assert extract_recipient(raw) == "wisevehicle"


def test_x_original_to_outranks_to():
    raw = (
        "From: jobs@board.example\r\n"
        "X-Original-To: wisevehicle@careercaddy.online\r\n"
        "To: dough@careercaddy.online\r\n"
        "\r\nbody\r\n"
    )
    assert extract_recipient(raw) == "wisevehicle"


def test_picks_caddy_address_among_multiple_recipients():
    """A To carrying several addresses returns the @careercaddy.online one,
    not whichever happens to be first."""
    raw = (
        "From: jobs@board.example\r\n"
        "To: someone@elsewhere.com, dough@careercaddy.online, other@x.io\r\n"
        "\r\nbody\r\n"
    )
    assert extract_recipient(raw) == "dough"


def test_display_name_form():
    raw = 'From: x@y.com\r\nTo: "Dough" <dough@careercaddy.online>\r\n\r\nbody\r\n'
    assert extract_recipient(raw) == "dough"


def test_case_insensitive_domain():
    raw = "From: x@y.com\r\nTo: Dough@CareerCaddy.Online\r\n\r\nbody\r\n"
    assert extract_recipient(raw) == "dough"


def test_bare_forwarding_localpart():
    """The bare catchall address resolves to localpart ``forwarding`` — which
    the owner gate then treats as no-user (no CC account) and drops."""
    raw = "From: x@y.com\r\nDelivered-To: forwarding@careercaddy.online\r\n\r\nbody\r\n"
    assert extract_recipient(raw) == "forwarding"


def test_none_when_no_caddy_recipient():
    """An original alert delivered to the operator's personal alias — no
    @careercaddy.online recipient anywhere — yields None."""
    raw = (
        "From: alerts@ziprecruiter.com\r\n"
        "Delivered-To: doug@passiveobserver.com\r\n"
        "To: doug@passiveobserver.com\r\n"
        "\r\nbody\r\n"
    )
    assert extract_recipient(raw) is None


def test_none_when_no_recipient_headers():
    raw = "From: x@y.com\r\nSubject: hi\r\n\r\nbody\r\n"
    assert extract_recipient(raw) is None
