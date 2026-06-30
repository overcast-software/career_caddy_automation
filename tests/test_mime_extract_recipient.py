"""AUTO-18 M1 — ``extract_recipient`` derives the catchall owner localpart.

The per-user catchall delivers every ``<username>@careercaddy.online`` message
into one maildir and stamps ``Delivered-To: forwarding@careercaddy.online`` (the
sink) on the way in; the triage owner gate keys on the localpart this message
was *actually* addressed to, which the original ``To`` carries. So
``extract_recipient`` scans the recipient headers in priority ``Delivered-To`` >
``X-Original-To`` > ``To``, **skips the catchall sink** (``forwarding``), and
returns the localpart of the first genuine ``@careercaddy.online`` address — or
``None`` when none is present (a personal-alias original, or only the bare sink;
the gate drops either without a JobPost).

No pytest-asyncio needed — ``extract_recipient`` is a pure function.
"""

from __future__ import annotations

from src.email_source.mime import extract_recipient


def test_to_header_localpart():
    raw = "From: jobs@board.example\r\nTo: dough@careercaddy.online\r\n\r\nbody\r\n"
    assert extract_recipient(raw) == "dough"


def test_catchall_delivered_to_skipped_real_user_in_to():
    """THE live case: the catchall stamps ``Delivered-To: forwarding@`` (sink)
    on every forward while the genuine target rides on ``To``. Resolution must
    skip the sink and return the real user — not ``forwarding``. (Regression:
    before the skip, every forward resolved to ``forwarding`` and dropped.)"""
    raw = (
        "From: jobs@board.example\r\n"
        "Delivered-To: forwarding@careercaddy.online\r\n"
        "To: wisevehicle@careercaddy.online\r\n"
        "\r\nbody\r\n"
    )
    assert extract_recipient(raw) == "wisevehicle"


def test_non_sink_delivered_to_wins_over_to():
    """For a self-hoster whose MTA stamps the *real* per-user target on
    Delivered-To (not the sink), envelope priority still holds: a non-sink
    Delivered-To outranks a cosmetic To."""
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


def test_bare_sink_only_is_none():
    """When the ONLY caddy recipient is the catchall sink (``forwarding@`` with
    no per-user To), there is no genuine owner — resolve to None so the gate
    drops it without a JobPost."""
    raw = "From: x@y.com\r\nDelivered-To: forwarding@careercaddy.online\r\n\r\nbody\r\n"
    assert extract_recipient(raw) is None


def test_sink_in_to_alongside_real_user():
    """Both the sink and a real user on the same To (cc'd) — return the real
    user, never the sink."""
    raw = (
        "From: x@y.com\r\n"
        "To: forwarding@careercaddy.online, samuelki@careercaddy.online\r\n"
        "\r\nbody\r\n"
    )
    assert extract_recipient(raw) == "samuelki"


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
