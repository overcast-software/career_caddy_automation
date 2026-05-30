"""Repo-anchored config locator.

cc_auto is *self-contained*: config + state live with the install, not
under ``~/.config`` or ``~/.local/share``. Anchoring runtime paths to
``CADDY_HOME`` keeps a contributor's workflow simple — clone, configure,
run; ``rm -rf`` the checkout removes everything.

``caddy_home()`` resolves the anchor:
1. ``$CADDY_HOME`` env var if set.
2. Walks up from this file looking for ``pyproject.toml``.
3. Falls back to the cwd (in editable installs the anchor is just
   the package root).

Downstream helpers (``caddy_log_dir``, ``caddy_var_dir``) compose paths
under the anchor.
"""

from __future__ import annotations

import os
from pathlib import Path


def caddy_home() -> Path:
    """Return the absolute anchor directory for cc_auto state.

    Not cached: the env var is the source of truth, and a cache makes
    test isolation harder (env-var monkeypatch in one test would bleed
    into the next). The filesystem walk is cheap.
    """
    env = os.environ.get("CADDY_HOME")
    if env:
        return Path(env).expanduser().resolve()

    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd().resolve()


def caddy_var_dir() -> Path:
    """``$CADDY_HOME/var/`` — runtime state (logs, mongo bind mounts).

    Creates the directory on first access. Whole tree is gitignored.
    """
    path = caddy_home() / "var"
    path.mkdir(parents=True, exist_ok=True)
    return path


def caddy_log_dir() -> Path:
    """``$CADDY_LOG_DIR`` env override or ``$CADDY_HOME/var/logs/``.

    File-log destination for the rotating handler in
    ``lib/observability.py``.
    """
    env = os.environ.get("CADDY_LOG_DIR")
    if env:
        path = Path(env).expanduser().resolve()
    else:
        path = caddy_var_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
