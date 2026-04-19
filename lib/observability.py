"""Shared logfire configuration for every entry point.

Call `configure_logfire("service-name")` as the first thing in each CLI.
It loads .env, configures logfire (silent if LOGFIRE_TOKEN unset), and
instruments pydantic-ai + httpx so agent runs and API calls show up as spans.

MCP stdio servers MUST keep stdout clean for JSON-RPC — this helper passes
`console=False` and `send_to_logfire="if-token-present"` so the unconfigured
case cannot pollute stdout.
"""

from __future__ import annotations

import os
from typing import Any

_configured = False


def configure_logfire(service_name: str, **extra: Any) -> None:
    """Idempotent logfire setup. Safe to call from anywhere; no-ops after first call."""
    global _configured
    if _configured:
        return
    _configured = True

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        import logfire
    except ImportError:
        return

    logfire.configure(
        service_name=service_name,
        console=False,
        send_to_logfire="if-token-present",
        **extra,
    )

    if os.environ.get("LOGFIRE_TOKEN"):
        try:
            logfire.instrument_pydantic_ai()
        except Exception:
            pass
        try:
            logfire.instrument_httpx()
        except Exception:
            pass
