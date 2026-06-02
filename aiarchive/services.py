from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from io import BytesIO
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from pathlib import PurePosixPath
from sqlite3 import Connection
from typing import Any

from .config import settings
from .db import recreate_fts_triggers
from .importers import IMPORTERS, get_importer
from .importers.common import stable_hash
from .models import NormalizedConversation, NormalizedMessage


@dataclass(slots=True)
class ImportResult:
    platform: str
    filename: str
    file_sha256: str
    conversations_seen: int
    messages_seen: int
    conversations_inserted: int
    messages_inserted: int


@dataclass(slots=True)
class PurgeResult:
    platform: str
    conversations_deleted: int
    messages_deleted: int
    imports_deleted: int


@dataclass(slots=True)
class ConversationPage:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.total <= 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def format_utc_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _local_date_start_to_utc(date_value: str) -> str:
    local_day = date.fromisoformat(date_value)
    local_start = datetime.combine(local_day, time.min).astimezone()
    return format_utc_datetime(local_start.astimezone(UTC))


def _local_date_end_exclusive_to_utc(date_value: str) -> str:
    local_day = date.fromisoformat(date_value) + timedelta(days=1)
    local_start = datetime.combine(local_day, time.min).astimezone()
    return format_utc_datetime(local_start.astimezone(UTC))


def supported_platforms() -> list[str]:
    return sorted(IMPORTERS.keys())


def import_file(connection: Connection, platform: str, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer(platform)
    if importer.platform == "openai" and _looks_like_zip_upload(filename, raw):
        return import_openai_export_zip(connection, filename, raw)
    if importer.platform == "gemini" and _looks_like_zip_upload(filename, raw):
        return import_gemini_takeout_zip(connection, filename, raw)
    if importer.platform == "grok" and _looks_like_zip_upload(filename, raw):
        return import_grok_export_zip(connection, filename, raw)
    if importer.platform == "deepseek" and _looks_like_zip_upload(filename, raw):
        return import_deepseek_export_zip(connection, filename, raw)
    if importer.platform == "claude" and _looks_like_zip_upload(filename, raw):
        return import_claude_export_zip(connection, filename, raw)
    if _looks_like_zip_upload(filename, raw):
        raise ValueError(f"{platform.capitalize()} import does not accept this ZIP format")
    file_sha = sha256_bytes(raw)
    conversations = _parse_import_bytes(importer, raw)
    _prepare_platform_assets(conversations, importer.platform, file_sha, None)
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_file_from_path(connection: Connection, platform: str, json_path: str) -> ImportResult:
    source_path = Path(json_path).expanduser().resolve()
    if not source_path.exists():
        raise ValueError(f"Import file not found: {json_path}")

    importer = get_importer(platform)
    if importer.platform == "openai":
        if source_path.is_dir():
            return import_openai_export_directory(connection, source_path.name, source_path)
        if source_path.suffix.lower() == ".zip":
            return import_openai_export_zip(connection, source_path.name, source_path.read_bytes())
        if _looks_like_openai_export_file(source_path.name):
            return import_openai_export_directory(connection, source_path.parent.name, source_path.parent)
    if importer.platform == "gemini" and source_path.suffix.lower() == ".zip":
        return import_gemini_takeout_zip(connection, source_path.name, source_path.read_bytes())
    if importer.platform == "grok" and source_path.suffix.lower() == ".zip":
        return import_grok_export_zip(connection, source_path.name, source_path.read_bytes())
    if importer.platform == "claude" and source_path.suffix.lower() == ".zip":
        return import_claude_export_zip(connection, source_path.name, source_path.read_bytes())
    if source_path.suffix.lower() == ".zip":
        raise ValueError(f"{platform.capitalize()} import does not accept this ZIP format")

    raw = source_path.read_bytes()
    file_sha = sha256_bytes(raw)
    conversations = _parse_import_bytes(importer, raw, source_path=source_path)
    _prepare_platform_assets(conversations, importer.platform, file_sha, source_path)
    return _persist_import(connection, importer.platform, source_path.name, file_sha, conversations)


def import_file_with_uploaded_assets(
    connection: Connection,
    platform: str,
    filename: str,
    raw: bytes,
    uploaded_assets: dict[str, bytes],
) -> ImportResult:
    importer = get_importer(platform)
    if importer.platform == "openai" and _looks_like_zip_upload(filename, raw):
        return import_openai_export_zip(connection, filename, raw)
    if importer.platform == "gemini" and _looks_like_zip_upload(filename, raw):
        return import_gemini_takeout_zip(connection, filename, raw)
    if importer.platform == "grok" and _looks_like_zip_upload(filename, raw):
        return import_grok_export_zip(connection, filename, raw)
    if importer.platform == "deepseek" and _looks_like_zip_upload(filename, raw):
        return import_deepseek_export_zip(connection, filename, raw)
    if importer.platform == "claude" and _looks_like_zip_upload(filename, raw):
        return import_claude_export_zip(connection, filename, raw)
    if _looks_like_zip_upload(filename, raw):
        raise ValueError(f"{platform.capitalize()} import does not accept this ZIP format")
    file_sha = sha256_bytes(raw)
    conversations = _parse_import_bytes(importer, raw)
    _prepare_platform_assets_from_uploads(conversations, importer.platform, file_sha, uploaded_assets)
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_openai_export_directory(connection: Connection, filename: str, source_root: Path) -> ImportResult:
    importer = get_importer("openai")
    export_root = _locate_openai_export_root(source_root)
    file_sha = stable_hash("openai_directory", str(export_root))
    payload = _load_openai_export_payload(export_root)
    source_marker = export_root / "export_manifest.json"
    if not source_marker.is_file():
        source_marker = export_root
    conversations = importer.parse_payload(payload, source_path=source_marker)
    _prepare_platform_assets(conversations, importer.platform, file_sha, source_marker)
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_openai_export_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("openai")
    file_sha = sha256_bytes(raw)

    _validate_zip_matches_platform("openai", raw)

    with tempfile.TemporaryDirectory(prefix="openai_export_", dir=settings.imports_dir) as temp_dir:
        extracted_root = Path(temp_dir)
        try:
            export_root = _extract_openai_export_zip(raw, extracted_root)
        except UnicodeDecodeError as exc:
            raise ValueError("Could not read filenames inside the OpenAI ZIP archive") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError("Uploaded OpenAI file is not a valid ZIP archive") from exc

        payload = _load_openai_export_payload(export_root)
        source_marker = export_root / "export_manifest.json"
        if not source_marker.is_file():
            source_marker = export_root
        conversations = importer.parse_payload(payload, source_path=source_marker)
        _prepare_platform_assets(conversations, importer.platform, file_sha, source_marker)
        return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_gemini_takeout_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("gemini")
    file_sha = sha256_bytes(raw)

    _validate_zip_matches_platform("gemini", raw)

    with tempfile.TemporaryDirectory(prefix="gemini_takeout_", dir=settings.imports_dir) as temp_dir:
        extracted_root = Path(temp_dir)
        try:
            json_path = _extract_gemini_takeout_zip(raw, extracted_root)
        except UnicodeDecodeError as exc:
            raise ValueError("Could not read filenames inside the Gemini Takeout ZIP archive") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError("Uploaded Gemini file is not a valid ZIP archive") from exc
        conversations = _parse_import_bytes(
            importer,
            json_path.read_bytes(),
            source_path=json_path,
            error_prefix="Gemini archive content is not in the expected export format",
        )
        _prepare_platform_assets(conversations, importer.platform, file_sha, json_path)
        return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_deepseek_export_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("deepseek")
    file_sha = sha256_bytes(raw)

    _validate_zip_matches_platform("deepseek", raw)

    try:
        conversations_raw = _read_deepseek_conversations_json(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("Could not read filenames inside the DeepSeek ZIP archive") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded DeepSeek file is not a valid ZIP archive") from exc

    conversations = _parse_import_bytes(
        importer,
        conversations_raw,
        error_prefix="DeepSeek archive content is not in the expected export format",
    )
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_claude_export_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("claude")
    file_sha = sha256_bytes(raw)

    _validate_zip_matches_platform("claude", raw)

    try:
        conversations_raw = _read_claude_conversations_json(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("Could not read filenames inside the Claude ZIP archive") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded Claude file is not a valid ZIP archive") from exc

    conversations = _parse_import_bytes(
        importer,
        conversations_raw,
        error_prefix="Claude archive content is not in the expected export format",
    )
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_grok_export_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("grok")
    file_sha = sha256_bytes(raw)

    _validate_zip_matches_platform("grok", raw)

    with tempfile.TemporaryDirectory(prefix="grok_export_", dir=settings.imports_dir) as temp_dir:
        extracted_root = Path(temp_dir)
        try:
            json_path = _extract_grok_export_zip(raw, extracted_root)
        except UnicodeDecodeError as exc:
            raise ValueError("Could not read filenames inside the Grok ZIP archive") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError("Uploaded Grok file is not a valid ZIP archive") from exc

        conversations = _parse_import_bytes(
            importer,
            json_path.read_bytes(),
            source_path=json_path,
            error_prefix="Grok archive content is not in the expected export format",
        )
        _prepare_platform_assets(conversations, importer.platform, file_sha, json_path)
        return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def _persist_import(
    connection: Connection,
    platform: str,
    filename: str,
    file_sha: str,
    conversations: list[NormalizedConversation],
) -> ImportResult:
    conversations_inserted = 0
    messages_inserted = 0
    messages_seen = 0

    for conversation in conversations:
        if conversation.platform != platform:
            raise ValueError(f"Conversation platform mismatch: expected {platform}, got {conversation.platform}")

        created = format_utc_datetime(conversation.created_at)
        updated = format_utc_datetime(conversation.updated_at)

        before = connection.total_changes
        connection.execute(
            """
            INSERT OR IGNORE INTO conversations (
                platform, source_conversation_id, title, created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                conversation.platform,
                conversation.source_conversation_id,
                conversation.title,
                created,
                updated,
                conversation.metadata_json,
            ),
        )
        if connection.total_changes > before:
            conversations_inserted += 1
        else:
            connection.execute(
                """
                UPDATE conversations
                SET
                    title = ?,
                    updated_at = CASE
                        WHEN ? > updated_at THEN ?
                        ELSE updated_at
                    END,
                    metadata_json = ?
                WHERE platform = ? AND source_conversation_id = ?
                """,
                (
                    conversation.title,
                    updated,
                    updated,
                    conversation.metadata_json,
                    conversation.platform,
                    conversation.source_conversation_id,
                ),
            )

        conversation_row = connection.execute(
            "SELECT id FROM conversations WHERE platform = ? AND source_conversation_id = ?",
            (conversation.platform, conversation.source_conversation_id),
        ).fetchone()
        if conversation_row is None:
            raise RuntimeError("Failed to resolve conversation id after insert")
        conversation_id = int(conversation_row["id"])

        for message in conversation.messages:
            messages_seen += 1
            created_at = format_utc_datetime(message.created_at)
            before = connection.total_changes
            connection.execute(
                """
                INSERT OR IGNORE INTO messages (
                    conversation_id, source_message_id, role, content, created_at,
                    sequence_no, message_hash, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    message.source_message_id,
                    message.role,
                    message.content,
                    created_at,
                    message.sequence_no,
                    _message_hash(conversation, message),
                    message.metadata_json,
                ),
            )
            if connection.total_changes > before:
                messages_inserted += 1
            else:
                # Existing messages are treated as immutable archive entries.
                # This keeps repeated imports idempotent and avoids unnecessary
                # FTS churn for already-archived records.
                continue

    connection.execute(
        """
        INSERT INTO imports (
            platform, filename, file_sha256, conversations_seen, messages_seen,
            conversations_inserted, messages_inserted
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            platform,
            filename,
            file_sha,
            len(conversations),
            messages_seen,
            conversations_inserted,
            messages_inserted,
        ),
    )

    return ImportResult(
        platform=platform,
        filename=filename,
        file_sha256=file_sha,
        conversations_seen=len(conversations),
        messages_seen=messages_seen,
        conversations_inserted=conversations_inserted,
        messages_inserted=messages_inserted,
    )


def _message_hash(conversation: NormalizedConversation, message: NormalizedMessage) -> str:
    return stable_hash(
        conversation.platform,
        conversation.source_conversation_id,
        message.source_message_id,
        message.created_at.isoformat(),
        message.role,
        message.content,
    )


def list_conversations(
    connection: Connection,
    query: str = "",
    platform: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    limit: int = 100,
) -> ConversationPage:
    page = max(page, 1)
    offset = (page - 1) * limit
    if query:
        fts_query = _build_fts_match_query(query)
        if _contains_cjk(query) or not fts_query:
            return _list_conversations_with_like_search(
                connection=connection,
                query=query,
                platform=platform,
                date_from=date_from,
                date_to=date_to,
                page=page,
                limit=limit,
                offset=offset,
            )
        return _list_conversations_with_search(
            connection=connection,
            query=fts_query,
            platform=platform,
            date_from=date_from,
            date_to=date_to,
            page=page,
            limit=limit,
            offset=offset,
        )

    params: list[Any] = []
    joins = ["LEFT JOIN messages m ON m.conversation_id = c.id"]
    where_clauses = ["1 = 1"]

    if platform:
        where_clauses.append("c.platform = ?")
        params.append(platform)

    if date_from:
        where_clauses.append("m.created_at >= ?")
        params.append(_local_date_start_to_utc(date_from))

    if date_to:
        where_clauses.append("m.created_at < ?")
        params.append(_local_date_end_exclusive_to_utc(date_to))

    count_sql = f"""
        SELECT COUNT(*) AS total
        FROM (
            SELECT c.id
            FROM conversations c
            {' '.join(joins)}
            WHERE {' AND '.join(where_clauses)}
            GROUP BY c.id
        ) AS counted
    """
    total_row = connection.execute(count_sql, params).fetchone()
    total = int(total_row["total"] if total_row else 0)

    sql = f"""
        SELECT
            c.id,
            c.platform,
            c.title,
            c.source_conversation_id,
            COALESCE(MIN(m.created_at), c.created_at) AS first_message_at,
            COALESCE(MAX(m.created_at), c.updated_at) AS latest_message_at,
            COUNT(m.id) AS message_count,
            NULL AS matched_message_id,
            NULL AS snippet
        FROM conversations c
        {' '.join(joins)}
        WHERE {' AND '.join(where_clauses)}
        GROUP BY c.id
        ORDER BY latest_message_at DESC, c.id DESC
        LIMIT ? OFFSET ?
    """
    data_params = [*params, limit, offset]
    rows = connection.execute(sql, data_params).fetchall()
    return ConversationPage(
        items=[dict(row) for row in rows],
        total=total,
        page=page,
        page_size=limit,
    )


def _list_conversations_with_like_search(
    connection: Connection,
    query: str,
    platform: str,
    date_from: str,
    date_to: str,
    page: int,
    limit: int,
    offset: int,
) -> ConversationPage:
    match_params: list[Any] = [query]
    match_where = ["instr(m.content, ?) > 0"]

    if platform:
        match_where.append("c.platform = ?")
        match_params.append(platform)

    if date_from:
        match_where.append("m.created_at >= ?")
        match_params.append(_local_date_start_to_utc(date_from))

    if date_to:
        match_where.append("m.created_at < ?")
        match_params.append(_local_date_end_exclusive_to_utc(date_to))

    count_sql = f"""
        SELECT COUNT(*) AS total
        FROM (
            SELECT c.id
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            WHERE {' AND '.join(match_where)}
            GROUP BY c.id
        ) AS counted
    """
    total_row = connection.execute(count_sql, match_params).fetchone()
    total = int(total_row["total"] if total_row else 0)

    sql = f"""
        WITH matched AS (
            SELECT
                c.id AS conversation_id,
                m.id AS message_id,
                m.created_at AS matched_created_at,
                CASE
                    WHEN instr(m.content, ?) <= 24 THEN substr(m.content, 1, 96)
                    ELSE '...' || substr(m.content, instr(m.content, ?) - 24, 96)
                END AS snippet
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            WHERE {' AND '.join(match_where)}
        ),
        ranked AS (
            SELECT
                conversation_id,
                message_id,
                matched_created_at,
                snippet,
                ROW_NUMBER() OVER (
                    PARTITION BY conversation_id
                    ORDER BY matched_created_at DESC, message_id DESC
                ) AS rank_no
            FROM matched
        )
        SELECT
            c.id,
            c.platform,
            c.title,
            c.source_conversation_id,
            COALESCE(MIN(m.created_at), c.created_at) AS first_message_at,
            COALESCE(MAX(m.created_at), c.updated_at) AS latest_message_at,
            COUNT(m.id) AS message_count,
            ranked.message_id AS matched_message_id,
            ranked.snippet AS snippet
        FROM conversations c
        JOIN ranked ON ranked.conversation_id = c.id AND ranked.rank_no = 1
        LEFT JOIN messages m ON m.conversation_id = c.id
        GROUP BY c.id, ranked.snippet
        ORDER BY latest_message_at DESC, c.id DESC
        LIMIT ? OFFSET ?
    """
    rows = connection.execute(sql, [query, query, *match_params, limit, offset]).fetchall()
    return ConversationPage(
        items=[dict(row) for row in rows],
        total=total,
        page=page,
        page_size=limit,
    )


def _contains_cjk(value: str) -> bool:
    for char in value:
        codepoint = ord(char)
        if 0x3400 <= codepoint <= 0x9FFF or 0xF900 <= codepoint <= 0xFAFF:
            return True
    return False


def _build_fts_match_query(value: str) -> str:
    phrases = [
        phrase
        for phrase in re.findall(r"\S+", value or "")
        if re.search(r"[A-Za-z0-9_]", phrase)
    ]
    if not phrases:
        return ""
    return " AND ".join(f'"{phrase.replace(chr(34), chr(34) * 2)}"' for phrase in phrases)


def _list_conversations_with_search(
    connection: Connection,
    query: str,
    platform: str,
    date_from: str,
    date_to: str,
    page: int,
    limit: int,
    offset: int,
) -> ConversationPage:
    match_params: list[Any] = [query]
    match_where = ["message_fts.content MATCH ?"]

    if platform:
        match_where.append("c.platform = ?")
        match_params.append(platform)

    if date_from:
        match_where.append("m.created_at >= ?")
        match_params.append(_local_date_start_to_utc(date_from))

    if date_to:
        match_where.append("m.created_at < ?")
        match_params.append(_local_date_end_exclusive_to_utc(date_to))

    count_sql = f"""
        SELECT COUNT(*) AS total
        FROM (
            SELECT c.id
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            JOIN message_fts ON message_fts.rowid = m.id
            WHERE {' AND '.join(match_where)}
            GROUP BY c.id
        ) AS counted
    """
    total_row = connection.execute(count_sql, match_params).fetchone()
    total = int(total_row["total"] if total_row else 0)

    sql = f"""
        WITH matched AS (
            SELECT
                c.id AS conversation_id,
                m.id AS message_id,
                m.created_at AS matched_created_at,
                snippet(message_fts, 0, '<mark>', '</mark>', ' ... ', 16) AS snippet
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            JOIN message_fts ON message_fts.rowid = m.id
            WHERE {' AND '.join(match_where)}
        ),
        ranked AS (
            SELECT
                conversation_id,
                message_id,
                matched_created_at,
                snippet,
                ROW_NUMBER() OVER (
                    PARTITION BY conversation_id
                    ORDER BY matched_created_at DESC, message_id DESC
                ) AS rank_no
            FROM matched
        )
        SELECT
            c.id,
            c.platform,
            c.title,
            c.source_conversation_id,
            COALESCE(MIN(m.created_at), c.created_at) AS first_message_at,
            COALESCE(MAX(m.created_at), c.updated_at) AS latest_message_at,
            COUNT(m.id) AS message_count,
            ranked.message_id AS matched_message_id,
            ranked.snippet AS snippet
        FROM conversations c
        JOIN ranked ON ranked.conversation_id = c.id AND ranked.rank_no = 1
        LEFT JOIN messages m ON m.conversation_id = c.id
        GROUP BY c.id, ranked.snippet
        ORDER BY latest_message_at DESC, c.id DESC
        LIMIT ? OFFSET ?
    """
    rows = connection.execute(sql, [*match_params, limit, offset]).fetchall()
    return ConversationPage(
        items=[dict(row) for row in rows],
        total=total,
        page=page,
        page_size=limit,
    )


def list_platform_overview(connection: Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            c.platform,
            COUNT(DISTINCT c.id) AS conversation_count,
            COUNT(m.id) AS message_count,
            MAX(m.created_at) AS latest_message_at
        FROM conversations c
        LEFT JOIN messages m ON m.conversation_id = c.id
        GROUP BY c.platform
        ORDER BY c.platform ASC
        """
    ).fetchall()

    overview_by_platform = {row["platform"]: dict(row) for row in rows}
    items: list[dict[str, Any]] = []
    for platform in supported_platforms():
        items.append(
            overview_by_platform.get(
                platform,
                {
                    "platform": platform,
                    "conversation_count": 0,
                    "message_count": 0,
                    "latest_message_at": None,
                },
            )
        )
    return items


def purge_platform_data(connection: Connection, platform: str) -> PurgeResult:
    normalized_platform = platform.lower()
    if normalized_platform not in supported_platforms():
        raise ValueError(f"Unsupported platform '{platform}'")

    counts = connection.execute(
        """
        SELECT
            COUNT(DISTINCT c.id) AS conversation_count,
            COUNT(m.id) AS message_count
        FROM conversations c
        LEFT JOIN messages m ON m.conversation_id = c.id
        WHERE c.platform = ?
        """,
        (normalized_platform,),
    ).fetchone()

    imports_count_row = connection.execute(
        "SELECT COUNT(*) AS import_count FROM imports WHERE platform = ?",
        (normalized_platform,),
    ).fetchone()

    connection.executescript(
        """
        DROP TRIGGER IF EXISTS messages_ai;
        DROP TRIGGER IF EXISTS messages_ad;
        DROP TRIGGER IF EXISTS messages_au;
        """
    )
    try:
        connection.execute(
            """
            DELETE FROM messages
            WHERE conversation_id IN (
                SELECT id
                FROM conversations
                WHERE platform = ?
            )
            """,
            (normalized_platform,),
        )
        connection.execute("DELETE FROM message_fts WHERE platform = ?", (normalized_platform,))
        connection.execute("DELETE FROM imports WHERE platform = ?", (normalized_platform,))
        connection.execute("DELETE FROM conversations WHERE platform = ?", (normalized_platform,))
        asset_dir = settings.media_dir / normalized_platform
        if asset_dir.exists():
            shutil.rmtree(asset_dir)
    finally:
        recreate_fts_triggers(connection)

    return PurgeResult(
        platform=normalized_platform,
        conversations_deleted=int(counts["conversation_count"] if counts else 0),
        messages_deleted=int(counts["message_count"] if counts else 0),
        imports_deleted=int(imports_count_row["import_count"] if imports_count_row else 0),
    )


def get_conversation_detail(connection: Connection, conversation_id: int) -> dict[str, Any] | None:
    conversation = connection.execute(
        """
        SELECT
            c.id,
            c.platform,
            c.title,
            c.source_conversation_id,
            c.created_at,
            c.updated_at,
            COUNT(m.id) AS message_count
        FROM conversations c
        LEFT JOIN messages m ON m.conversation_id = c.id
        WHERE c.id = ?
        GROUP BY c.id
        """,
        (conversation_id,),
    ).fetchone()
    if conversation is None:
        return None

    messages = connection.execute(
        """
        SELECT id, role, content, created_at, sequence_no, metadata_json
        FROM messages
        WHERE conversation_id = ?
        ORDER BY created_at ASC, sequence_no ASC, id ASC
        """,
        (conversation_id,),
    ).fetchall()
    normalized_messages: list[dict[str, Any]] = []
    for row in messages:
        payload = dict(row)
        metadata = _load_metadata(payload.get("metadata_json"))
        payload["html_content"] = metadata.get("html_content", "")
        payload["display_content"] = metadata.get("display_content", payload.get("content", ""))
        payload["attachments"] = metadata.get("attachments", [])
        payload["attachment_count"] = len(payload["attachments"])
        normalized_messages.append(payload)
    return {"conversation": dict(conversation), "messages": normalized_messages}


def get_platform_browse_location(
    connection: Connection,
    conversation_id: int,
    page_size: int = 100,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        WITH ranked AS (
            SELECT
                c.id,
                c.platform,
                COALESCE(MAX(m.created_at), c.updated_at) AS latest_message_at
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
        ),
        target AS (
            SELECT id, platform, latest_message_at
            FROM ranked
            WHERE id = ?
        )
        SELECT
            target.id,
            target.platform,
            target.latest_message_at,
            (
                SELECT COUNT(*)
                FROM ranked other
                WHERE other.platform = target.platform
                  AND (
                      other.latest_message_at > target.latest_message_at
                      OR (other.latest_message_at = target.latest_message_at AND other.id > target.id)
                  )
            ) AS items_before
        FROM target
        """,
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None

    items_before = int(row["items_before"] or 0)
    page = (items_before // page_size) + 1
    return {
        "platform": str(row["platform"]),
        "page": page,
        "page_size": page_size,
        "anchor": f"conversation-{conversation_id}",
    }


def list_recent_imports(connection: Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT platform, filename, imported_at, conversations_seen, messages_seen,
               conversations_inserted, messages_inserted
        FROM imports
        ORDER BY imported_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _parse_import_bytes(
    importer: Any,
    raw: bytes,
    source_path: Path | None = None,
    error_prefix: str | None = None,
) -> list[NormalizedConversation]:
    try:
        return importer.parse_bytes(raw, source_path=source_path)
    except ValueError as exc:
        if error_prefix:
            raise ValueError(f"{error_prefix}: {exc}") from exc
        raise


def _load_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _looks_like_zip_upload(filename: str, raw: bytes) -> bool:
    if raw.startswith(b"PK\x03\x04") or raw.startswith(b"PK\x05\x06") or raw.startswith(b"PK\x07\x08"):
        return True
    return Path(filename or "").suffix.lower() == ".zip"


def _looks_like_openai_export_file(filename: str) -> bool:
    normalized = Path(filename or "").name.casefold()
    if normalized == "export_manifest.json":
        return True
    if normalized == "conversations.json":
        return True
    return bool(re.fullmatch(r"conversations-\d+\.json", normalized))


def _zip_contains_platform_signature(platform: str, raw: bytes) -> bool:
    try:
        with _open_zip_with_fallbacks(raw) as archive:
            member_names = [PurePosixPath(info.filename).name for info in archive.infolist() if not info.is_dir()]
    except (UnicodeDecodeError, zipfile.BadZipFile, ValueError):
        return False

    if platform == "gemini":
        return any(_is_gemini_activity_json_name(name) for name in member_names)
    if platform == "deepseek":
        names = {name.casefold() for name in member_names}
        return "conversations.json" in names and not ({"users.json", "memories.json"} & names)
    if platform == "claude":
        names = {name.casefold() for name in member_names}
        return "conversations.json" in names and bool({"users.json", "memories.json"} & names)
    if platform == "grok":
        return "prod-grok-backend.json" in member_names
    if platform == "openai":
        return any(_looks_like_openai_export_file(name) for name in member_names)
    return False


def _validate_zip_matches_platform(platform: str, raw: bytes) -> None:
    if not _looks_like_zip_upload("", raw):
        raise ValueError("Uploaded file is not a valid ZIP archive")

    if not _zip_contains_platform_signature(platform, raw):
        if platform == "gemini":
            raise ValueError("This ZIP does not look like a Gemini Takeout export")
        if platform == "deepseek":
            raise ValueError("This ZIP does not look like a DeepSeek export")
        if platform == "claude":
            raise ValueError("This ZIP does not look like a Claude export")
        if platform == "grok":
            raise ValueError("This ZIP does not look like a Grok export")
        if platform == "openai":
            raise ValueError("This ZIP does not look like an OpenAI ChatGPT export")
        raise ValueError("This ZIP does not match the selected platform")


def _is_gemini_activity_json_name(filename: str) -> bool:
    normalized = (filename or "").strip().casefold()
    if not normalized.endswith(".json"):
        return False
    stems = {
        "my activity.json",
        "我的活动记录.json",
        "gemini apps activity.json",
        "gemini activity.json",
    }
    if normalized in {name.casefold() for name in stems}:
        return True
    return "activity" in normalized or "活动" in normalized


def _is_gemini_activity_json(member_path: PurePosixPath) -> bool:
    if not _is_gemini_activity_json_name(member_path.name):
        return False

    parent_parts = [part.casefold() for part in member_path.parts[:-1]]
    return "gemini apps" in parent_parts


def _extract_gemini_takeout_zip(raw: bytes, destination_root: Path) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    extracted_json_path: Path | None = None

    with _open_zip_with_fallbacks(raw) as archive:
        for info in archive.infolist():
            member_path = PurePosixPath(info.filename)
            if info.is_dir() or not member_path.parts:
                continue

            relative_path = Path(*member_path.parts)
            target_path = (destination_root / relative_path).resolve()
            if destination_root.resolve() not in target_path.parents and target_path != destination_root.resolve():
                raise ValueError("ZIP contains unsafe path entries")

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target_path.open("wb") as target:
                shutil.copyfileobj(source, target)

            if _is_gemini_activity_json(member_path):
                extracted_json_path = target_path

    if extracted_json_path is None:
        raise ValueError("Gemini Takeout ZIP is missing the expected Gemini Apps activity JSON file")

    return extracted_json_path


def _extract_openai_export_zip(raw: bytes, destination_root: Path) -> Path:
    _extract_zip_to_directory(raw, destination_root)
    return _locate_openai_export_root(destination_root)


def _locate_openai_export_root(source_root: Path) -> Path:
    if source_root.is_file():
        source_root = source_root.parent

    manifest_matches = [path for path in source_root.rglob("export_manifest.json") if path.is_file()]
    if manifest_matches:
        manifest_matches.sort()
        return manifest_matches[0].parent

    conversation_matches = [path for path in source_root.rglob("conversations.json") if path.is_file()]
    conversation_matches.extend(path for path in source_root.rglob("conversations-*.json") if path.is_file())
    if conversation_matches:
        conversation_matches.sort()
        return conversation_matches[0].parent

    raise ValueError("OpenAI export is missing export_manifest.json or conversations JSON shards")


def _load_openai_export_payload(export_root: Path) -> list[Any]:
    conversations: list[Any] = []

    manifest_path = export_root / "export_manifest.json"
    manifest = _read_json_file(manifest_path) if manifest_path.is_file() else {}
    logical_files = manifest.get("logical_files") if isinstance(manifest, dict) else None

    shard_names: list[str] = []
    if isinstance(logical_files, dict):
        conversations_entry = logical_files.get("conversations.json")
        if isinstance(conversations_entry, dict):
            shard_names = [str(name) for name in conversations_entry.get("files") or [] if str(name).strip()]

    if not shard_names:
        shard_names = [path.name for path in sorted(export_root.glob("conversations-*.json")) if path.is_file()]

    if not shard_names and (export_root / "conversations.json").is_file():
        shard_names = ["conversations.json"]

    if not shard_names:
        raise ValueError("OpenAI export is missing conversation JSON shards")

    for shard_name in shard_names:
        shard_path = export_root / shard_name
        if not shard_path.is_file():
            raise ValueError(f"OpenAI export is missing shard file: {shard_name}")
        shard_payload = _read_json_file(shard_path)
        if isinstance(shard_payload, list):
            conversations.extend(shard_payload)
        elif isinstance(shard_payload, dict):
            conversations.extend(shard_payload.get("conversations") or shard_payload.get("items") or [shard_payload])
        else:
            raise ValueError(f"OpenAI shard has unsupported JSON shape: {shard_name}")

    return conversations


def _extract_grok_export_zip(raw: bytes, destination_root: Path) -> Path:
    _extract_zip_to_directory(raw, destination_root)
    matches = [path for path in destination_root.rglob("prod-grok-backend.json") if path.is_file()]
    if not matches:
        raise ValueError("Grok ZIP is missing prod-grok-backend.json")
    matches.sort()
    return matches[0]


def _read_deepseek_conversations_json(raw: bytes) -> bytes:
    with _open_zip_with_fallbacks(raw) as archive:
        for info in archive.infolist():
            member_path = PurePosixPath(info.filename)
            if info.is_dir() or not member_path.parts:
                continue
            if len(member_path.parts) != 1:
                continue
            if member_path.name != "conversations.json":
                continue
            with archive.open(info, "r") as source:
                return source.read()

    raise ValueError("DeepSeek ZIP is missing conversations.json at the archive root")


def _read_claude_conversations_json(raw: bytes) -> bytes:
    with _open_zip_with_fallbacks(raw) as archive:
        for info in archive.infolist():
            member_path = PurePosixPath(info.filename)
            if info.is_dir() or not member_path.parts:
                continue
            if len(member_path.parts) != 1:
                continue
            if member_path.name.casefold() != "conversations.json":
                continue
            with archive.open(info, "r") as source:
                return source.read()

    raise ValueError("Claude ZIP is missing conversations.json at the archive root")


def _extract_zip_to_directory(raw: bytes, destination_root: Path) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    with _open_zip_with_fallbacks(raw) as archive:
        for info in archive.infolist():
            member_path = PurePosixPath(info.filename)
            if not member_path.parts:
                continue

            target_path = (destination_root / Path(*member_path.parts)).resolve()
            if destination_root.resolve() not in target_path.parents and target_path != destination_root.resolve():
                raise ValueError("ZIP contains unsafe path entries")

            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target_path.open("wb") as target:
                shutil.copyfileobj(source, target)


@contextmanager
def _open_zip_with_fallbacks(raw: bytes):
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
        archive.infolist()
    except UnicodeDecodeError:
        repaired = _clear_zip_utf8_flags(raw)
        fallback_error: Exception | None = None
        for encoding in ("gb18030", "utf-8", "cp437"):
            try:
                archive = zipfile.ZipFile(BytesIO(repaired), metadata_encoding=encoding)
                archive.infolist()
                break
            except (UnicodeDecodeError, zipfile.BadZipFile) as exc:
                fallback_error = exc
        else:
            raise fallback_error or ValueError("Could not decode ZIP archive metadata")
    try:
        yield archive
    finally:
        archive.close()


def _clear_zip_utf8_flags(raw: bytes) -> bytes:
    patched = bytearray(raw)
    utf8_flag = 0x0800
    signatures = (
        (b"PK\x03\x04", 6),
        (b"PK\x01\x02", 8),
    )

    for signature, flag_offset in signatures:
        search_from = 0
        while True:
            index = patched.find(signature, search_from)
            if index < 0:
                break
            field_start = index + flag_offset
            if field_start + 2 <= len(patched):
                flags = int.from_bytes(patched[field_start : field_start + 2], "little")
                flags &= ~utf8_flag
                patched[field_start : field_start + 2] = flags.to_bytes(2, "little")
            search_from = index + 1

    return bytes(patched)


def _prepare_platform_assets(
    conversations: list[NormalizedConversation],
    platform: str,
    file_sha: str,
    source_path: Path | None,
) -> None:
    if platform == "openai":
        _prepare_openai_assets(conversations, file_sha, source_path)
        return

    if platform == "grok":
        _prepare_grok_assets(conversations, file_sha, source_path)
        return

    if platform != "gemini":
        return

    for conversation in conversations:
        for message in conversation.messages:
            metadata = _load_metadata(message.metadata_json)
            existing_attachments = metadata.get("attachments", [])
            if isinstance(existing_attachments, list) and existing_attachments:
                metadata["attachments"] = _copy_named_attachments(
                    platform=platform,
                    file_sha=file_sha,
                    source_path=source_path,
                    conversation_id=conversation.source_conversation_id,
                    attachments=existing_attachments,
                )
            elif "attachments" not in metadata:
                metadata["attachments"] = []

            html_content = metadata.get("html_content")
            if isinstance(html_content, str) and html_content.strip():
                html_attachments: list[dict[str, str]] = []
                if source_path is not None:
                    html_content, html_attachments = _copy_gemini_assets(
                        platform=platform,
                        file_sha=file_sha,
                        source_path=source_path,
                        conversation_id=conversation.source_conversation_id,
                        html_content=html_content,
                    )
                metadata["html_content"] = html_content
                if html_attachments:
                    merged = list(metadata.get("attachments", []))
                    known_urls = {item.get("url") for item in merged if isinstance(item, dict)}
                    for attachment in html_attachments:
                        if attachment.get("url") not in known_urls:
                            merged.append(attachment)
                    metadata["attachments"] = merged

            message.metadata_json = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _prepare_openai_assets(
    conversations: list[NormalizedConversation],
    file_sha: str,
    source_path: Path | None,
) -> None:
    if source_path is None:
        return

    export_root = source_path.parent if source_path.is_file() else source_path
    file_lookup, label_lookup = _build_openai_asset_lookup(export_root)

    for conversation in conversations:
        for message in conversation.messages:
            metadata = _load_metadata(message.metadata_json)
            existing_attachments = metadata.get("attachments", [])
            if isinstance(existing_attachments, list) and existing_attachments:
                metadata["attachments"] = _copy_openai_attachments(
                    file_sha=file_sha,
                    conversation_id=conversation.source_conversation_id,
                    attachments=existing_attachments,
                    file_lookup=file_lookup,
                    label_lookup=label_lookup,
                )
            elif "attachments" not in metadata:
                metadata["attachments"] = []

            message.metadata_json = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _build_openai_asset_lookup(export_root: Path) -> tuple[dict[str, Path], dict[str, str]]:
    file_lookup: dict[str, Path] = {}
    label_lookup: dict[str, str] = {}

    manifest_path = export_root / "export_manifest.json"
    manifest = _read_json_file(manifest_path) if manifest_path.is_file() else {}
    logical_files = manifest.get("logical_files") if isinstance(manifest, dict) else {}

    name_map_path = export_root / "conversation_asset_file_names.json"
    if name_map_path.is_file():
        raw_name_map = _read_json_file(name_map_path)
        if isinstance(raw_name_map, dict):
            for key, value in raw_name_map.items():
                key_text = str(key).strip()
                value_text = str(value).strip()
                if key_text and value_text:
                    label_lookup[key_text] = value_text
                    label_lookup[Path(key_text).stem] = value_text

    for path in export_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(export_root).as_posix()
        file_lookup.setdefault(relative, path)
        file_lookup.setdefault(path.name, path)
        file_lookup.setdefault(path.stem, path)

    if isinstance(logical_files, dict):
        for logical_name, entry in logical_files.items():
            if not isinstance(entry, dict):
                continue
            files = entry.get("files") or []
            for file_name in files:
                candidate = export_root / str(file_name)
                if not candidate.is_file():
                    continue
                logical_text = str(logical_name).strip()
                if logical_text:
                    file_lookup.setdefault(logical_text, candidate)
                    file_lookup.setdefault(Path(logical_text).name, candidate)
                    file_lookup.setdefault(Path(logical_text).stem, candidate)

    return file_lookup, label_lookup


def _copy_openai_attachments(
    file_sha: str,
    conversation_id: str,
    attachments: list[Any],
    file_lookup: dict[str, Path],
    label_lookup: dict[str, str],
) -> list[dict[str, str]]:
    target_dir = settings.media_dir / "openai" / file_sha / conversation_id
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, str]] = []
    seen: set[str] = set()
    used_names: set[str] = set()

    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue

        source_id = str(attachment.get("source_id") or attachment.get("filename") or "").strip()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)

        source_file = _resolve_openai_attachment_source(source_id, attachment, file_lookup)
        label = _resolve_openai_attachment_label(source_id, attachment, source_file, label_lookup)

        if source_file is None or not source_file.is_file():
            copied.append(
                {
                    "filename": source_id,
                    "label": label,
                    "url": "",
                }
            )
            continue

        saved_name = _unique_attachment_name(
            _choose_openai_saved_name(source_id, label, attachment, source_file),
            used_names,
        )
        target_file = target_dir / saved_name
        if not target_file.exists():
            shutil.copy2(source_file, target_file)

        copied.append(
            {
                "filename": saved_name,
                "label": label,
                "url": f"/media/openai/{file_sha}/{conversation_id}/{saved_name}",
            }
        )
    return copied


def _resolve_openai_attachment_source(
    source_id: str,
    attachment: dict[str, Any],
    file_lookup: dict[str, Path],
) -> Path | None:
    candidates = [
        str(attachment.get("filename") or "").strip(),
        str(attachment.get("source_name") or "").strip(),
        str(attachment.get("source_path") or "").strip(),
        source_id,
        f"{source_id}.dat" if not source_id.endswith(".dat") else "",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        normalized = candidate.replace("\\", "/").strip()
        path = file_lookup.get(normalized) or file_lookup.get(Path(normalized).name) or file_lookup.get(Path(normalized).stem)
        if path is not None and path.is_file():
            return path
    return None


def _resolve_openai_attachment_label(
    source_id: str,
    attachment: dict[str, Any],
    source_file: Path | None,
    label_lookup: dict[str, str],
) -> str:
    label = str(attachment.get("label") or "").strip()
    if label:
        return Path(label).name or label

    lookup_candidates = [source_id]
    if source_file is not None:
        lookup_candidates.extend([source_file.name, source_file.stem])

    for candidate in lookup_candidates:
        mapped = label_lookup.get(candidate)
        if mapped:
            return Path(mapped).name or mapped

    if source_file is not None:
        return source_file.name
    return source_id


def _choose_openai_saved_name(
    source_id: str,
    label: str,
    attachment: dict[str, Any],
    source_file: Path,
) -> str:
    preferred = Path(label).name.strip() or source_file.name
    preferred = _sanitize_attachment_name(preferred)

    mime_type = str(attachment.get("mime_type") or "").strip()
    if Path(preferred).suffix:
        return preferred

    extension = _guess_extension_from_mime(mime_type) or _guess_extension_from_source(source_file)
    if extension:
        return f"{preferred}{extension}"

    return f"{preferred}{source_file.suffix}" if source_file.suffix else preferred


def _sanitize_attachment_name(value: str) -> str:
    cleaned = Path(value).name.strip()
    cleaned = re.sub(r"[<>:\"/\\\\|?*\x00-\x1f]", "_", cleaned)
    return cleaned or "attachment"


def _unique_attachment_name(value: str, used_names: set[str]) -> str:
    candidate = value
    stem = Path(value).stem
    suffix = Path(value).suffix
    index = 2
    while candidate.casefold() in used_names:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used_names.add(candidate.casefold())
    return candidate


def _guess_extension_from_mime(value: str) -> str:
    if not value:
        return ""
    guessed = mimetypes.guess_extension(value, strict=False) or ""
    if guessed == ".jpe":
        return ".jpg"
    return guessed


def _guess_extension_from_source(source_file: Path) -> str:
    if source_file.suffix and source_file.suffix.lower() != ".dat":
        return source_file.suffix
    try:
        return _detect_attachment_extension(source_file.read_bytes())
    except OSError:
        return ""


def _prepare_grok_assets(
    conversations: list[NormalizedConversation],
    file_sha: str,
    source_path: Path | None,
) -> None:
    if source_path is None:
        return

    asset_root = source_path.parent / "prod-mc-asset-server"
    for conversation in conversations:
        for message in conversation.messages:
            metadata = _load_metadata(message.metadata_json)
            existing_attachments = metadata.get("attachments", [])
            if isinstance(existing_attachments, list) and existing_attachments:
                metadata["attachments"] = _copy_grok_attachments(
                    file_sha=file_sha,
                    asset_root=asset_root,
                    conversation_id=conversation.source_conversation_id,
                    attachments=existing_attachments,
                )
            elif "attachments" not in metadata:
                metadata["attachments"] = []

            message.metadata_json = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _prepare_platform_assets_from_uploads(
    conversations: list[NormalizedConversation],
    platform: str,
    file_sha: str,
    uploaded_assets: dict[str, bytes],
) -> None:
    if platform != "gemini":
        return

    normalized_assets = {Path(name).name: content for name, content in uploaded_assets.items() if name}

    for conversation in conversations:
        for message in conversation.messages:
            metadata = _load_metadata(message.metadata_json)
            existing_attachments = metadata.get("attachments", [])
            if isinstance(existing_attachments, list) and existing_attachments:
                metadata["attachments"] = _store_uploaded_attachments(
                    platform=platform,
                    file_sha=file_sha,
                    conversation_id=conversation.source_conversation_id,
                    attachments=existing_attachments,
                    uploaded_assets=normalized_assets,
                )
            elif "attachments" not in metadata:
                metadata["attachments"] = []

            html_content = metadata.get("html_content")
            if isinstance(html_content, str) and html_content.strip():
                html_content, html_attachments = _store_uploaded_html_assets(
                    platform=platform,
                    file_sha=file_sha,
                    conversation_id=conversation.source_conversation_id,
                    html_content=html_content,
                    uploaded_assets=normalized_assets,
                )
                metadata["html_content"] = html_content
                if html_attachments:
                    merged = list(metadata.get("attachments", []))
                    known_urls = {item.get("url") for item in merged if isinstance(item, dict)}
                    for attachment in html_attachments:
                        if attachment.get("url") not in known_urls:
                            merged.append(attachment)
                    metadata["attachments"] = merged

            message.metadata_json = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _copy_gemini_assets(
    platform: str,
    file_sha: str,
    source_path: Path,
    conversation_id: str,
    html_content: str,
) -> tuple[str, list[dict[str, str]]]:
    source_dir = source_path.parent
    target_dir = settings.media_dir / platform / file_sha
    target_dir.mkdir(parents=True, exist_ok=True)

    attachments: list[dict[str, str]] = []
    copied_by_name: dict[str, str] = {}
    seen: set[str] = set()

    def replace_match(match: re.Match[str]) -> str:
        attribute = match.group(1)
        quote = match.group(2)
        raw_path = match.group(3).strip()
        normalized_name = Path(raw_path).name
        if not normalized_name or normalized_name in seen:
            if normalized_name:
                return f'{attribute}={quote}{copied_by_name.get(normalized_name, raw_path)}{quote}'
            return match.group(0)

        source_file = source_dir / normalized_name
        if not source_file.is_file():
            return match.group(0)

        safe_name = source_file.name
        target_file = target_dir / safe_name
        if not target_file.exists():
            shutil.copy2(source_file, target_file)

        relative_url = f"/media/{platform}/{file_sha}/{safe_name}"
        copied_by_name[normalized_name] = relative_url
        seen.add(normalized_name)
        attachments.append({"filename": safe_name, "url": relative_url})
        return f'{attribute}={quote}{relative_url}{quote}'

    rewritten_html = re.sub(r'(src|href)=(["\'])([^"\']+)\2', replace_match, html_content, flags=re.IGNORECASE)
    return rewritten_html, attachments


def _copy_named_attachments(
    platform: str,
    file_sha: str,
    source_path: Path | None,
    conversation_id: str,
    attachments: list[Any],
) -> list[dict[str, str]]:
    if source_path is None:
        return [attachment for attachment in attachments if isinstance(attachment, dict)]

    source_dir = source_path.parent
    target_dir = settings.media_dir / platform / file_sha
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, str]] = []
    seen: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        filename = str(attachment.get("filename") or "").strip()
        if not filename or filename in seen:
            continue
        seen.add(filename)

        source_file = source_dir / Path(filename).name
        if not source_file.is_file():
            copied.append(
                {
                    "filename": filename,
                    "label": str(attachment.get("label") or filename),
                    "url": "",
                }
            )
            continue

        target_file = target_dir / source_file.name
        if not target_file.exists():
            shutil.copy2(source_file, target_file)
        copied.append(
            {
                "filename": source_file.name,
                "label": str(attachment.get("label") or source_file.name),
                "url": f"/media/{platform}/{file_sha}/{source_file.name}",
            }
        )
    return copied


def _copy_grok_attachments(
    file_sha: str,
    asset_root: Path,
    conversation_id: str,
    attachments: list[Any],
) -> list[dict[str, str]]:
    target_dir = settings.media_dir / "grok" / file_sha / conversation_id
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, str]] = []
    seen: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue

        attachment_id = str(attachment.get("filename") or "").strip()
        if not attachment_id or attachment_id in seen:
            continue
        seen.add(attachment_id)

        source_file = asset_root / attachment_id / "content"
        label = str(attachment.get("label") or attachment_id)
        if not source_file.is_file():
            copied.append(
                {
                    "filename": attachment_id,
                    "label": label,
                    "url": "",
                }
            )
            continue

        extension = _detect_attachment_extension(source_file.read_bytes())
        saved_name = f"{attachment_id}{extension}"
        target_file = target_dir / saved_name
        if not target_file.exists():
            shutil.copy2(source_file, target_file)

        copied.append(
            {
                "filename": saved_name,
                "label": label,
                "url": f"/media/grok/{file_sha}/{conversation_id}/{saved_name}",
            }
        )
    return copied


def _detect_attachment_extension(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return ".webp"
    if raw.startswith(b"%PDF-"):
        return ".pdf"
    if raw.startswith(b"PK\x03\x04"):
        return ".zip"
    if raw.startswith(b"ID3") or raw[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return ".mp3"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return ".wav"
    if raw.startswith(b"OggS"):
        return ".ogg"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return ".mp4"

    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return ".bin"
    return ".txt"


def _store_uploaded_attachments(
    platform: str,
    file_sha: str,
    conversation_id: str,
    attachments: list[Any],
    uploaded_assets: dict[str, bytes],
) -> list[dict[str, str]]:
    target_dir = settings.media_dir / platform / file_sha / conversation_id
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, str]] = []
    seen: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        filename = str(attachment.get("filename") or "").strip()
        if not filename or filename in seen:
            continue
        seen.add(filename)

        asset_name = Path(filename).name
        raw = uploaded_assets.get(asset_name)
        if raw is None:
            copied.append(
                {
                    "filename": asset_name,
                    "label": str(attachment.get("label") or asset_name),
                    "url": "",
                }
            )
            continue

        target_file = target_dir / asset_name
        if not target_file.exists():
            target_file.write_bytes(raw)

        copied.append(
            {
                "filename": asset_name,
                "label": str(attachment.get("label") or asset_name),
                "url": f"/media/{platform}/{file_sha}/{conversation_id}/{asset_name}",
            }
        )
    return copied


def _store_uploaded_html_assets(
    platform: str,
    file_sha: str,
    conversation_id: str,
    html_content: str,
    uploaded_assets: dict[str, bytes],
) -> tuple[str, list[dict[str, str]]]:
    target_dir = settings.media_dir / platform / file_sha / conversation_id
    target_dir.mkdir(parents=True, exist_ok=True)

    attachments: list[dict[str, str]] = []
    copied_by_name: dict[str, str] = {}
    seen: set[str] = set()

    def replace_match(match: re.Match[str]) -> str:
        attribute = match.group(1)
        quote = match.group(2)
        raw_path = match.group(3).strip()
        normalized_name = Path(raw_path).name
        if not normalized_name or normalized_name in seen:
            if normalized_name:
                return f'{attribute}={quote}{copied_by_name.get(normalized_name, raw_path)}{quote}'
            return match.group(0)

        raw = uploaded_assets.get(normalized_name)
        if raw is None:
            return match.group(0)

        target_file = target_dir / normalized_name
        if not target_file.exists():
            target_file.write_bytes(raw)

        relative_url = f"/media/{platform}/{file_sha}/{conversation_id}/{normalized_name}"
        copied_by_name[normalized_name] = relative_url
        seen.add(normalized_name)
        attachments.append({"filename": normalized_name, "url": relative_url})
        return f'{attribute}={quote}{relative_url}{quote}'

    rewritten_html = re.sub(r'(src|href)=(["\'])([^"\']+)\2', replace_match, html_content, flags=re.IGNORECASE)
    return rewritten_html, attachments
