"""SQLite schema and connection helpers.

The physical schema is fixed and shared by every generated application:
business models live in metadata (``models`` / ``model_versions``) and record
values live in ``records.data_json``. That is what lets the AI create and
change models without running DDL.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SAVEPOINT_COUNTER = itertools.count(1)


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Make one engine write atomic on its own.

    A savepoint works both standalone and nested inside a caller's transaction
    (an Action or an HTTP request), so a validation failure can never leave a
    half-written record behind - while still letting the caller roll back the
    whole operation as a single unit.
    """
    name = f"sp_zooagent_{next(_SAVEPOINT_COUNTER)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT,
    color TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    active_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    application_id TEXT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'application',
    instructions TEXT,
    greeting TEXT,
    model_profile_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_application ON agents(application_id, role);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    application_id TEXT,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_agent ON conversations(agent_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS agent_memories (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'application',
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_agent ON agent_memories(agent_id, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    application_id TEXT,
    agent_id TEXT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    schedule_json TEXT,
    payload_json TEXT,
    misfire_policy TEXT NOT NULL DEFAULT 'skip',
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    checkpoint_json TEXT,
    result_json TEXT,
    error TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id, started_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    application_id TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_application ON audit_logs(application_id, created_at);

CREATE TABLE IF NOT EXISTS tool_permissions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    policy TEXT NOT NULL DEFAULT 'allow',
    created_at TEXT NOT NULL
);
"""

APPLICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    model_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    active_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    model_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    PRIMARY KEY (model_id, version)
);

CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_model ON records(model_id, deleted_at);

CREATE TABLE IF NOT EXISTS record_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    from_record TEXT NOT NULL,
    field_key TEXT NOT NULL,
    to_record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relations_from ON record_relations(from_record, field_key);
CREATE INDEX IF NOT EXISTS idx_relations_to ON record_relations(to_record);

CREATE TABLE IF NOT EXISTS record_unique_keys (
    model_id TEXT NOT NULL,
    field_key TEXT NOT NULL,
    value TEXT NOT NULL,
    record_id TEXT NOT NULL,
    PRIMARY KEY (model_id, field_key, value)
);

CREATE TABLE IF NOT EXISTS record_history (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    data_json TEXT,
    created_at TEXT NOT NULL,
    actor TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_record ON record_history(record_id, revision);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    active_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_versions (
    action_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (action_id, version)
);

CREATE TABLE IF NOT EXISTS ui_pages (
    id TEXT PRIMARY KEY,
    page_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    icon TEXT,
    schema_json TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_evolution (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    changes_json TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane, local-first pragmas.

    A connection per operation keeps things thread-safe without a pool; SQLite
    connection setup is cheap and this is a single-user desktop app.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_system_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SYSTEM_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
        )


def init_application_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(APPLICATION_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO app_meta(key, value) VALUES ('schema_version', '1')"
        )
