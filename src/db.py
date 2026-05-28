"""Lightweight SQLite store for caddy-inbox run history.

Schema
------
triage_runs  — one row per run_once() call
triage_emails — one row per email processed

DB path: $CADDY_TRIAGE_DB, default ~/.local/share/career_caddy/inbox_triage.db
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_DB = Path.home() / ".local" / "share" / "career_caddy" / "inbox_triage.db"

_DDL = """
CREATE TABLE IF NOT EXISTS triage_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    backend      TEXT,
    total_emails INTEGER,
    counters     TEXT
);
CREATE TABLE IF NOT EXISTS triage_emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES triage_runs(id),
    email_id     TEXT NOT NULL,
    subject      TEXT,
    outcome      TEXT,
    tags_added   TEXT,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage_skipped_duplicates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES triage_runs(id),
    email_id         TEXT NOT NULL,
    incoming_title   TEXT,
    incoming_company TEXT,
    incoming_link    TEXT,
    matched_post_id  INTEGER,
    confidence       REAL,
    reason           TEXT,
    source           TEXT,
    recorded_at      TEXT NOT NULL
);
"""


def open_db() -> sqlite3.Connection:
    path = Path(os.environ.get("CADDY_TRIAGE_DB", str(_DEFAULT_DB)))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_DDL)
    con.commit()
    return con


def start_run(con: sqlite3.Connection, backend: str | None) -> int:
    cur = con.execute(
        "INSERT INTO triage_runs (started_at, backend) VALUES (?, ?)",
        (datetime.now(UTC).isoformat(), backend or "notmuch"),
    )
    con.commit()
    return cur.lastrowid  # type: ignore[return-value]


def record_email(
    con: sqlite3.Connection,
    run_id: int,
    email_id: str,
    subject: str | None,
    outcome: str,
    tags_added: list[str],
) -> None:
    con.execute(
        "INSERT INTO triage_emails"
        " (run_id, email_id, subject, outcome, tags_added, processed_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            email_id,
            subject,
            outcome,
            ",".join(tags_added),
            datetime.now(UTC).isoformat(),
        ),
    )
    con.commit()


def record_skipped_duplicate(
    con: sqlite3.Connection,
    run_id: int,
    email_id: str,
    *,
    incoming_title: str | None,
    incoming_company: str | None,
    incoming_link: str | None,
    matched_post_id: int | None,
    confidence: float | None,
    reason: str | None,
    source: str | None,
) -> None:
    """Record a JobPost the dedup pre-pass skipped instead of POSTing.

    One row per skipped post — keeps the matched post id / confidence /
    reason that the per-email `outcome` string can't carry, so a
    false-positive audit is a single SELECT."""
    con.execute(
        "INSERT INTO triage_skipped_duplicates"
        " (run_id, email_id, incoming_title, incoming_company, incoming_link,"
        "  matched_post_id, confidence, reason, source, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            email_id,
            incoming_title,
            incoming_company,
            incoming_link,
            matched_post_id,
            confidence,
            reason,
            source,
            datetime.now(UTC).isoformat(),
        ),
    )
    con.commit()


def finish_run(
    con: sqlite3.Connection,
    run_id: int,
    total_emails: int,
    counters: dict[str, int],
) -> None:
    con.execute(
        "UPDATE triage_runs SET finished_at=?, total_emails=?, counters=? WHERE id=?",
        (
            datetime.now(UTC).isoformat(),
            total_emails,
            json.dumps(counters),
            run_id,
        ),
    )
    con.commit()
