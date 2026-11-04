"""SQLite persistence for the control plane.

One connection per request (``flask.g``) with WAL enabled.  Every write goes
through :func:`transaction`, which takes the write lock immediately so two
pollers can never claim the same task.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id          TEXT PRIMARY KEY,
    server_sn         TEXT NOT NULL DEFAULT '',
    hostname          TEXT NOT NULL DEFAULT '',
    agent_version     TEXT NOT NULL DEFAULT '',
    protocol_version  INTEGER NOT NULL DEFAULT 1,
    reported_status   TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    hardware_json     TEXT NOT NULL DEFAULT '{}',
    boot_media_json   TEXT NOT NULL DEFAULT '{}',
    storage_json      TEXT NOT NULL DEFAULT '{}',
    current_task_json TEXT NOT NULL DEFAULT '{}',
    queue_depth       INTEGER NOT NULL DEFAULT 0,
    queue_max         INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT NOT NULL DEFAULT '',
    first_seen_at     TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    last_seen_ip      TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agents_server_sn
    ON agents(server_sn) WHERE server_sn <> '';
CREATE INDEX IF NOT EXISTS ix_agents_heartbeat ON agents(last_heartbeat_at DESC);

CREATE TABLE IF NOT EXISTS tasks (
    task_id              TEXT PRIMARY KEY,
    type                 TEXT NOT NULL,
    agent_id             TEXT NOT NULL REFERENCES agents(agent_id),
    server_sn            TEXT NOT NULL,
    params_json          TEXT NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL,
    phase                TEXT NOT NULL DEFAULT 'received',
    lease_id             TEXT,
    lease_expires_at     TEXT,
    cancel_requested     INTEGER NOT NULL DEFAULT 0,
    require_local_confirm INTEGER NOT NULL DEFAULT 1,
    timeout_seconds      INTEGER NOT NULL DEFAULT 600,
    priority             INTEGER NOT NULL DEFAULT 100,
    attempts             INTEGER NOT NULL DEFAULT 0,
    created_by           TEXT NOT NULL DEFAULT 'ui',
    created_at           TEXT NOT NULL,
    started_at           TEXT,
    finished_at          TEXT,
    expires_at           TEXT NOT NULL,
    error_code           TEXT,
    error_message        TEXT,
    result_json          TEXT NOT NULL DEFAULT '{}',
    progress_json        TEXT NOT NULL DEFAULT '{}',
    evidence_status      TEXT NOT NULL DEFAULT 'none',
    last_idempotency_key TEXT,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_claim ON tasks(agent_id, status, priority, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS ix_tasks_type ON tasks(type);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    phase      TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    data_json  TEXT NOT NULL DEFAULT '{}',
    actor      TEXT NOT NULL DEFAULT 'server',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_task ON task_events(task_id, id);

CREATE TABLE IF NOT EXISTS evidence (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    filename          TEXT NOT NULL,
    rel_path          TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    size              INTEGER NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'other',
    stage             TEXT NOT NULL DEFAULT 'execute',
    content_type      TEXT NOT NULL DEFAULT 'application/octet-stream',
    collected_at      TEXT NOT NULL DEFAULT '',
    manifest_index    INTEGER NOT NULL DEFAULT 0,
    received_at       TEXT NOT NULL,
    uploader_agent_id TEXT NOT NULL DEFAULT '',
    UNIQUE(task_id, filename)
);
CREATE INDEX IF NOT EXISTS ix_evidence_task ON evidence(task_id);

CREATE TABLE IF NOT EXISTS controller_snapshots (
    agent_id         TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    controller_id    TEXT NOT NULL,
    pds_json         TEXT NOT NULL DEFAULT '[]',
    vds_json         TEXT NOT NULL DEFAULT '[]',
    pds_collected_at TEXT NOT NULL DEFAULT '',
    vds_collected_at TEXT NOT NULL DEFAULT '',
    topology_hash    TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (agent_id, controller_id)
);
CREATE INDEX IF NOT EXISTS ix_snapshots_agent ON controller_snapshots(agent_id);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(settings) -> None:
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["SETTINGS"].db_path)
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE with a small retry budget for busy databases."""

    for attempt in range(3):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


__all__ = ["SCHEMA", "connect", "init_db", "get_db", "close_db", "transaction"]
