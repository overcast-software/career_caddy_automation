"""Shared test fixtures.

The auto-scrape env gates are cleared before every test so the suite
never inherits the operator's live ``.env`` (python-dotenv loads it at
import time, and the Dagger CI context copies the automation dir —
``.env`` included — into the test container). Without this, setting
``CADDY_AUTO_SCRAPE=1`` on the operator box flips the per-post
precedence inside ``_create_posts_from_urls`` and breaks the
known-good-gate tests (seen live 2026-07-09, CC-133). Tests that
exercise a gate set it explicitly with ``monkeypatch.setenv``.
"""

import pytest

_AUTO_SCRAPE_GATES = (
    "CADDY_AUTO_SCRAPE",
    "CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD",
)


@pytest.fixture(autouse=True)
def _isolate_auto_scrape_gates(monkeypatch):
    for var in _AUTO_SCRAPE_GATES:
        monkeypatch.delenv(var, raising=False)
