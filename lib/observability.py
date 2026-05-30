"""Shared logfire + file-log configuration for every entry point.

Call `configure_logfire("service-name")` as the first thing in each CLI.
It loads .env, configures logfire (silent if LOGFIRE_TOKEN unset),
instruments pydantic-ai + httpx so agent runs and API calls show up as
spans, AND attaches a rotating file handler to the root logger so the
service's stdout output also lands in
``$CADDY_LOG_DIR/{service-name}.log`` (default
``$CADDY_HOME/var/logs/``).

The file log decouples post-mortem visibility from the TTY a daemon
was launched on — Phase A2 of the cc_auto roadmap.

MCP stdio servers MUST keep stdout clean for JSON-RPC — this helper
passes ``console=False`` and ``send_to_logfire="if-token-present"`` so
the unconfigured case cannot pollute stdout. The file handler is
opt-out via ``CADDY_FILE_LOG=0`` for the same MCP stdio cases that
shouldn't write to disk either.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any

_configured = False

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_LOG_BACKUP_COUNT = 5


def _attach_file_handler(service_name: str) -> None:
    """Attach a rotating file handler to the root logger.

    Idempotent — checks ``_caddy_file_handler`` marker on existing handlers
    so re-imports don't double-up. Failure (e.g., read-only fs) is
    swallowed: file logging is best-effort, never a precondition.

    Important: attaching ANY handler to the root logger turns
    ``logging.basicConfig()`` into a no-op (it only acts when the root has
    zero handlers). The cc_auto CLIs do their own ``basicConfig`` for
    stdout right after this call, so to keep stdout output alive we also
    attach a ``StreamHandler`` here. The duplicate-call guard against
    pre-existing console handlers preserves the usual idempotence.
    """
    if os.environ.get("CADDY_FILE_LOG", "1") == "0":
        return
    root = logging.getLogger()
    has_file_handler = any(getattr(h, "_caddy_file_handler", False) for h in root.handlers)
    has_stream_handler = any(getattr(h, "_caddy_stream_handler", False) for h in root.handlers)

    # Bump level so handlers see INFO from pipelines.
    if root.level == 0 or root.level > logging.INFO:
        root.setLevel(logging.INFO)

    if not has_stream_handler:
        # Stdout passthrough — without this, attaching the file handler
        # would silence the terminal because basicConfig in the entry
        # script no-ops when handlers already exist.
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_LOG_FORMAT))
        stream.setLevel(logging.INFO)
        stream._caddy_stream_handler = True  # type: ignore[attr-defined]
        root.addHandler(stream)

    if has_file_handler:
        return
    try:
        from src.config import caddy_log_dir

        log_path = caddy_log_dir() / f"{service_name}.log"
        handler = RotatingFileHandler(
            log_path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler.setLevel(logging.INFO)
        handler._caddy_file_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    except Exception:
        # Best-effort. Don't let a misconfigured log dir crash the CLI.
        pass


def configure_logfire(service_name: str, **extra: Any) -> None:
    """Idempotent logfire + file-log setup. Safe to call from anywhere; no-ops after first call."""
    global _configured
    if _configured:
        return
    _configured = True

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    _attach_file_handler(service_name)

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
