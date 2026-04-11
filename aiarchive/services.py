from __future__ import annotations

import hashlib
import json
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
    if importer.platform == "gemini" and _looks_like_zip_upload(filename, raw):
        return import_gemini_takeout_zip(connection, filename, raw)
    if importer.platform == "grok" and _looks_like_zip_upload(filename, raw):
        return import_grok_export_zip(connection, filename, raw)
    if importer.platform == "deepseek" and _looks_like_zip_upload(filename, raw):
        return import_deepseek_export_zip(connection, filename, raw)
    file_sha = sha256_bytes(raw)
    conversations = importer.parse_bytes(raw)
    _prepare_platform_assets(conversations, importer.platform, file_sha, None)
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_file_from_path(connection: Connection, platform: str, json_path: str) -> ImportResult:
    source_path = Path(json_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Import file not found: {json_path}")

    importer = get_importer(platform)
    if importer.platform == "gemini" and source_path.suffix.lower() == ".zip":
        return import_gemini_takeout_zip(connection, source_path.name, source_path.read_bytes())
    if importer.platform == "grok" and source_path.suffix.lower() == ".zip":
        return import_grok_export_zip(connection, source_path.name, source_path.read_bytes())

    raw = source_path.read_bytes()
    file_sha = sha256_bytes(raw)
    conversations = importer.parse_bytes(raw, source_path=source_path)
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
    if importer.platform == "gemini" and _looks_like_zip_upload(filename, raw):
        return import_gemini_takeout_zip(connection, filename, raw)
    if importer.platform == "grok" and _looks_like_zip_upload(filename, raw):
        return import_grok_export_zip(connection, filename, raw)
    if importer.platform == "deepseek" and _looks_like_zip_upload(filename, raw):
        return import_deepseek_export_zip(connection, filename, raw)
    file_sha = sha256_bytes(raw)
    conversations = importer.parse_bytes(raw)
    _prepare_platform_assets_from_uploads(conversations, importer.platform, file_sha, uploaded_assets)
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_gemini_takeout_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("gemini")
    file_sha = sha256_bytes(raw)

    with tempfile.TemporaryDirectory(prefix="gemini_takeout_", dir=settings.imports_dir) as temp_dir:
        extracted_root = Path(temp_dir)
        try:
            json_path = _extract_gemini_takeout_zip(raw, extracted_root)
        except UnicodeDecodeError as exc:
            raise ValueError("Could not read filenames inside the Gemini Takeout ZIP archive") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError("Uploaded Gemini file is not a valid ZIP archive") from exc
        conversations = importer.parse_bytes(json_path.read_bytes(), source_path=json_path)
        _prepare_platform_assets(conversations, importer.platform, file_sha, json_path)
        return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_deepseek_export_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("deepseek")
    file_sha = sha256_bytes(raw)

    try:
        conversations_raw = _read_deepseek_conversations_json(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("Could not read filenames inside the DeepSeek ZIP archive") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded DeepSeek file is not a valid ZIP archive") from exc

    conversations = importer.parse_bytes(conversations_raw)
    return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def import_grok_export_zip(connection: Connection, filename: str, raw: bytes) -> ImportResult:
    importer = get_importer("grok")
    file_sha = sha256_bytes(raw)

    with tempfile.TemporaryDirectory(prefix="grok_export_", dir=settings.imports_dir) as temp_dir:
        extracted_root = Path(temp_dir)
        try:
            json_path = _extract_grok_export_zip(raw, extracted_root)
        except UnicodeDecodeError as exc:
            raise ValueError("Could not read filenames inside the Grok ZIP archive") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError("Uploaded Grok file is not a valid ZIP archive") from exc

        conversations = importer.parse_bytes(json_path.read_bytes(), source_path=json_path)
        _prepare_platform_assets(conversations, importer.platform, file_sha, json_path)
        return _persist_import(connection, importer.platform, filename, file_sha, conversations)


def extract_uploaded_zip(filename: str, raw: bytes) -> Path:
    archive_name = Path(filename or "upload.zip").stem or "upload"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", archive_name).strip("-._") or "upload"
    extract_root = settings.data_dir / "zip_uploads" / f"{safe_name}-{sha256_bytes(raw)[:12]}"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)

    try:
        _extract_zip_to_directory(raw, extract_root)
    except UnicodeDecodeError as exc:
        raise ValueError("Could not read filenames inside the ZIP archive") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded file is not a valid ZIP archive") from exc

    return extract_root


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
                    format_utc_datetime(message.created_at),
                    message.sequence_no,
                    _message_hash(conversation, message),
                    message.metadata_json,
                ),
            )
            if connection.total_changes > before:
                messages_inserted += 1
            else:
                connection.execute(
                    """
                    UPDATE messages
                    SET
                        role = ?,
                        content = ?,
                        created_at = ?,
                        sequence_no = ?,
                        metadata_json = ?
                    WHERE conversation_id = ? AND source_message_id = ?
                    """,
                    (
                        message.role,
                        message.content,
                        format_utc_datetime(message.created_at),
                        message.sequence_no,
                        message.metadata_json,
                        conversation_id,
                        message.source_message_id,
                    ),
                )

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
        if _contains_cjk(query):
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
            query=query,
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

        connection.execute("DELETE FROM imports WHERE platform = ?", (normalized_platform,))
        connection.execute("DELETE FROM conversations WHERE platform = ?", (normalized_platform,))
        connection.execute("INSERT INTO message_fts(message_fts) VALUES ('rebuild')")
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


def _load_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _looks_like_zip_upload(filename: str, raw: bytes) -> bool:
    if raw.startswith(b"PK\x03\x04") or raw.startswith(b"PK\x05\x06") or raw.startswith(b"PK\x07\x08"):
        return True
    return Path(filename or "").suffix.lower() == ".zip"


def _extract_gemini_takeout_zip(raw: bytes, destination_root: Path) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    expected_parts = (
        "Takeout",
        "\u6211\u7684\u6d3b\u52a8",
        "Gemini Apps",
    )
    json_filename = "\u6211\u7684\u6d3b\u52a8\u8bb0\u5f55.json"
    extracted_json_path: Path | None = None

    with _open_zip_with_fallbacks(raw) as archive:
        for info in archive.infolist():
            member_path = PurePosixPath(info.filename)
            if info.is_dir() or not member_path.parts:
                continue

            if tuple(member_path.parts[: len(expected_parts)]) != expected_parts:
                continue

            relative_path = Path(*member_path.parts)
            target_path = (destination_root / relative_path).resolve()
            if destination_root.resolve() not in target_path.parents and target_path != destination_root.resolve():
                raise ValueError("ZIP contains unsafe path entries")

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target_path.open("wb") as target:
                shutil.copyfileobj(source, target)

            if member_path.name == json_filename:
                extracted_json_path = target_path

    if extracted_json_path is None:
        raise ValueError("Gemini Takeout ZIP is missing the expected Gemini Apps activity JSON file")

    return extracted_json_path


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

        source_file = source_dir / normalized_name
        if not source_file.is_file():
            return match.group(0)

        safe_name = source_file.name
        target_file = target_dir / safe_name
        if not target_file.exists():
            shutil.copy2(source_file, target_file)

        relative_url = f"/media/{platform}/{file_sha}/{conversation_id}/{safe_name}"
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
                "url": f"/media/{platform}/{file_sha}/{conversation_id}/{source_file.name}",
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
