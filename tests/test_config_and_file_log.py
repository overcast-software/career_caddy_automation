"""Phase A2 — file log + config anchor.

caddy_home walks up to find pyproject.toml; CADDY_LOG_DIR overrides
the default. The rotating file handler is attached idempotently.
"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path


def test_caddy_home_finds_repo_root(monkeypatch):
    monkeypatch.delenv("CADDY_HOME", raising=False)
    from src import config

    importlib.reload(config)
    home = config.caddy_home()
    # Repo root is the directory containing pyproject.toml.
    assert (home / "pyproject.toml").is_file()


def test_caddy_home_env_override(monkeypatch, tmp_path):
    target = tmp_path / "fake_home"
    target.mkdir()
    monkeypatch.setenv("CADDY_HOME", str(target))
    from src import config

    importlib.reload(config)
    assert config.caddy_home() == target.resolve()


def test_caddy_log_dir_default_under_var(monkeypatch, tmp_path):
    target = tmp_path / "fake_home"
    target.mkdir()
    monkeypatch.setenv("CADDY_HOME", str(target))
    # Ignore any .env override developers have locally — this asserts
    # the "no env override" default path.
    monkeypatch.delenv("CADDY_LOG_DIR", raising=False)
    # load_dotenv may have set it; explicitly null it after the
    # monkeypatch fixture so import-time .env loads can't bleed in.
    os.environ.pop("CADDY_LOG_DIR", None)
    from src import config

    importlib.reload(config)
    log_dir = config.caddy_log_dir()
    assert log_dir == (target / "var" / "logs").resolve()
    assert log_dir.is_dir()


def test_caddy_log_dir_env_override(monkeypatch, tmp_path):
    target = tmp_path / "logs_here"
    monkeypatch.setenv("CADDY_LOG_DIR", str(target))
    from src import config

    importlib.reload(config)
    assert config.caddy_log_dir() == target.resolve()
    assert target.is_dir()


def test_file_handler_attached_once(monkeypatch, tmp_path):
    """The file handler is idempotent — re-invoking configure_logfire
    must not double-up."""
    monkeypatch.setenv("CADDY_HOME", str(tmp_path))
    monkeypatch.setenv("CADDY_FILE_LOG", "1")
    # Force the test's tmp_path destination explicitly; otherwise
    # load_dotenv() picks up any CADDY_LOG_DIR in the developer's local
    # .env and bypasses CADDY_HOME.
    monkeypatch.setenv("CADDY_LOG_DIR", str(tmp_path / "var" / "logs"))
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)

    # Force a clean reload of the module so _configured starts False
    # and our module-level state is repeatable across tests.
    from lib import observability

    importlib.reload(observability)
    # Strip any pre-existing caddy handlers from prior test runs.
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_caddy_file_handler", False)]

    observability.configure_logfire("test-service")
    handlers_after_first = [h for h in root.handlers if getattr(h, "_caddy_file_handler", False)]
    assert len(handlers_after_first) == 1
    # File should land under tmp_path/var/logs/test-service.log. The
    # handler's baseFilename is the source of truth; assert against it
    # so a test running in a parallel context (pytest cache, prior
    # CADDY_HOME) reports the actual destination instead of guessing.
    fh = handlers_after_first[0]
    log_file = Path(fh.baseFilename)
    assert log_file.parent == tmp_path / "var" / "logs"
    logger = logging.getLogger("test-service-logger")
    logger.info("hello from rotating handler")
    fh.flush()
    assert log_file.is_file()
    contents = log_file.read_text()
    assert "hello from rotating handler" in contents

    # Re-invocation is a no-op because _configured is True; reset
    # the guard and try again to prove the handler-marker dedupe also works.
    observability._configured = False
    observability.configure_logfire("test-service")
    handlers_after_second = [h for h in root.handlers if getattr(h, "_caddy_file_handler", False)]
    assert len(handlers_after_second) == 1


def test_file_handler_opt_out(monkeypatch, tmp_path):
    """CADDY_FILE_LOG=0 disables the handler — for MCP stdio servers."""
    monkeypatch.setenv("CADDY_HOME", str(tmp_path))
    monkeypatch.setenv("CADDY_FILE_LOG", "0")
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)

    from lib import observability

    importlib.reload(observability)
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_caddy_file_handler", False)]

    observability.configure_logfire("opted-out-service")
    handlers = [h for h in root.handlers if getattr(h, "_caddy_file_handler", False)]
    assert handlers == []


def _cleanup_handlers():
    """Helper for any test that wants to leave the root logger clean."""
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_caddy_file_handler", False)]


# Always tidy after the file-handler tests so the rest of the suite isn't
# logging into tmp_path-owned files.
def teardown_function(_):
    _cleanup_handlers()
    # Restore default level so other tests don't see lingering side-effects.
    logging.getLogger().setLevel(logging.WARNING)
    # Clear the cwd / env caches so subsequent tests pick up fresh state.
    os.environ.pop("CADDY_HOME", None)
    os.environ.pop("CADDY_LOG_DIR", None)
