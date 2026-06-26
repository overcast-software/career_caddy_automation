"""Cached pymongo client + indexed database handle.

One ``MongoClient`` per process — pymongo's client is already a
connection pool, so re-creating it on every write would just churn
sockets.

Connection URI: ``$MONGODB_URI``, default
``mongodb://localhost:27017/cc_auto``. Database name is extracted
from the URI path when present; otherwise we fall back to ``cc_auto``.

Indexes are declared once on first ``get_db()`` call. ``create_index``
is idempotent — replays on re-import are cheap no-ops.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_URI = "mongodb://localhost:27017/cc_auto"
DEFAULT_DB_NAME = "cc_auto"

# Explicit opt-out values for ``CADDY_OBSERVABILITY``. Observability is
# enabled by default (unset / any other value); only these disable it.
_OBSERVABILITY_OFF_VALUES = {"0", "false", "no", "off"}


def _db_name_from_uri(uri: str) -> str:
    """Extract the database name from a Mongo connection URI.

    Mongo URIs put the db in the path: ``mongodb://host:27017/<db>``.
    Empty / missing path → ``cc_auto`` fallback.
    """
    parsed = urlparse(uri)
    name = parsed.path.lstrip("/")
    return name or DEFAULT_DB_NAME


@lru_cache(maxsize=1)
def get_db():
    """Return the cached ``pymongo.database.Database``, or ``None`` when
    observability is disabled.

    Setting ``CADDY_OBSERVABILITY`` to an off-value (``0``/``false``/``no``/
    ``off``, case- and whitespace-insensitive) short-circuits before pymongo
    is imported or any connection is attempted — so no 2s server-selection
    timeout and no "mongo unreachable" warning when Mongo is absent. Unset
    (the default) or any other value keeps observability enabled.

    Lazy-imports pymongo so test environments that don't need observability
    can skip the dependency. Builds indexes on first call.
    """
    if os.environ.get("CADDY_OBSERVABILITY", "").strip().lower() in _OBSERVABILITY_OFF_VALUES:
        logger.debug("observability disabled via CADDY_OBSERVABILITY; skipping mongo")
        return None

    from pymongo import ASCENDING, MongoClient

    uri = os.environ.get("MONGODB_URI", DEFAULT_URI)
    db_name = _db_name_from_uri(uri)
    client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=2000)
    db = client[db_name]

    # Idempotent index declarations. Re-runs are free.
    db.triage_emails.create_index([("run_id", ASCENDING)])
    db.triage_emails.create_index([("email_id", ASCENDING)])
    db.triage_runs.create_index([("started_at", ASCENDING)])
    db.skipped_duplicates.create_index([("run_id", ASCENDING)])
    db.skipped_duplicates.create_index([("email_id", ASCENDING)])

    logger.debug("mongo connected: uri=%s db=%s", uri, db_name)
    return db


def reset_cache() -> None:
    """Test helper — clears the cached connection so a different URI
    can be injected via env var."""
    get_db.cache_clear()
