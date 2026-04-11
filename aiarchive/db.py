from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA foreign_keys=ON;")
    connection.execute("PRAGMA synchronous=NORMAL;")
    connection.execute("PRAGMA temp_store=MEMORY;")
    return connection


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connection = _connect(settings.database_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


FTS_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO message_fts(rowid, content, role, platform, created_at, message_id)
    VALUES (
        NEW.id,
        NEW.content,
        NEW.role,
        (SELECT platform FROM conversations WHERE id = NEW.conversation_id),
        NEW.created_at,
        NEW.id
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, content, role, platform, created_at, message_id)
    VALUES(
        'delete',
        OLD.id,
        OLD.content,
        OLD.role,
        (SELECT platform FROM conversations WHERE id = OLD.conversation_id),
        OLD.created_at,
        OLD.id
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, content, role, platform, created_at, message_id)
    VALUES(
        'delete',
        OLD.id,
        OLD.content,
        OLD.role,
        (SELECT platform FROM conversations WHERE id = OLD.conversation_id),
        OLD.created_at,
        OLD.id
    );
    INSERT INTO message_fts(rowid, content, role, platform, created_at, message_id)
    VALUES (
        NEW.id,
        NEW.content,
        NEW.role,
        (SELECT platform FROM conversations WHERE id = NEW.conversation_id),
        NEW.created_at,
        NEW.id
    );
END;
"""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, source_conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    message_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(conversation_id, source_message_id),
    UNIQUE(message_hash)
);

CREATE INDEX IF NOT EXISTS idx_conversations_platform_updated
    ON conversations(platform, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages(conversation_id, created_at, sequence_no);

CREATE INDEX IF NOT EXISTS idx_messages_created_at
    ON messages(created_at);

CREATE INDEX IF NOT EXISTS idx_messages_role
    ON messages(role);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    conversations_seen INTEGER NOT NULL,
    messages_seen INTEGER NOT NULL,
    conversations_inserted INTEGER NOT NULL,
    messages_inserted INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_imports_platform_imported
    ON imports(platform, imported_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    content,
    role UNINDEXED,
    platform UNINDEXED,
    created_at UNINDEXED,
    message_id UNINDEXED,
    tokenize = 'unicode61'
);
"""


def initialize_database() -> None:
    with get_connection() as connection:
        connection.executescript(SCHEMA_SQL)
        connection.executescript(FTS_TRIGGER_SQL)


def recreate_fts_triggers(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS messages_ai;
        DROP TRIGGER IF EXISTS messages_ad;
        DROP TRIGGER IF EXISTS messages_au;
        """
    )
    connection.executescript(FTS_TRIGGER_SQL)
