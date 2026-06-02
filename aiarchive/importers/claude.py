from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..models import NormalizedConversation, NormalizedMessage
from .base import BaseImporter
from .common import coerce_datetime, ensure_list, extract_text, json_dumps, stable_hash


class ClaudeImporter(BaseImporter):
    platform = "claude"

    def parse_payload(self, payload: Any, source_path: Any = None) -> list[NormalizedConversation]:
        conversations: list[NormalizedConversation] = []
        for item in ensure_list(payload):
            if not isinstance(item, dict):
                continue
            conversations.append(self._parse_conversation(item))
        return conversations

    def _parse_conversation(self, item: dict[str, Any]) -> NormalizedConversation:
        conversation_id = extract_text(item.get("uuid")) or stable_hash(json_dumps(item))
        title = self._normalize_title(item.get("name") or item.get("summary"))
        created_at = coerce_datetime(item.get("created_at") or datetime.now(UTC))
        updated_at = coerce_datetime(item.get("updated_at") or created_at, fallback=created_at)

        messages: list[NormalizedMessage] = []
        raw_messages = item.get("chat_messages") or item.get("messages") or []
        for raw_message in self._sort_messages(raw_messages):
            if not isinstance(raw_message, dict):
                continue

            role = self._map_role(raw_message.get("sender") or raw_message.get("role"))
            if role not in {"user", "assistant"}:
                continue

            display_content = self._extract_display_content(raw_message)
            attachments = self._extract_attachments(raw_message)
            searchable_content = self._build_search_text(display_content, attachments)
            if not searchable_content and not attachments:
                continue

            message_created = coerce_datetime(raw_message.get("created_at") or created_at, fallback=created_at)
            message_id = extract_text(raw_message.get("uuid")) or stable_hash(
                conversation_id,
                str(len(messages) + 1),
                role,
                searchable_content,
                message_created.isoformat(),
            )

            messages.append(
                NormalizedMessage(
                    source_message_id=message_id,
                    role=role,
                    content=searchable_content,
                    created_at=message_created,
                    sequence_no=len(messages) + 1,
                    metadata_json=json_dumps(
                        {
                            "display_content": display_content,
                            "attachments": attachments,
                            "parent_message_uuid": raw_message.get("parent_message_uuid"),
                            "raw_sender": raw_message.get("sender"),
                            "content_types": self._content_types(raw_message.get("content")),
                        }
                    ),
                )
            )

        if messages:
            created_at = min(message.created_at for message in messages)
            updated_at = max(message.created_at for message in messages)

        return NormalizedConversation(
            platform=self.platform,
            source_conversation_id=conversation_id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            metadata_json=json_dumps(
                {
                    "export_format": "claude",
                    "summary": item.get("summary"),
                    "account": item.get("account"),
                }
            ),
            messages=messages,
        )

    @staticmethod
    def _normalize_title(value: Any) -> str:
        title = extract_text(value)
        return title or "Untitled conversation"

    @staticmethod
    def _map_role(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "human":
            return "user"
        if normalized in {"assistant", "user"}:
            return normalized
        return normalized or "unknown"

    def _extract_display_content(self, message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, list):
            parts = [self._extract_content_block_text(block) for block in content]
            text = "\n\n".join(part for part in parts if part).strip()
            if text:
                return text

        return extract_text(message.get("text"))

    def _extract_content_block_text(self, block: Any) -> str:
        if not isinstance(block, dict):
            return extract_text(block)

        block_type = extract_text(block.get("type")).lower()
        if block_type == "text":
            return extract_text(block.get("text"))

        if block_type == "tool_use":
            name = extract_text(block.get("name")) or "tool"
            input_text = extract_text(block.get("input"))
            if input_text:
                return f"[Tool use: {name}]\n{input_text}"
            return f"[Tool use: {name}]"

        if block_type == "tool_result":
            name = extract_text(block.get("name")) or extract_text(block.get("tool_use_id")) or "tool"
            result_text = extract_text(block.get("content")) or extract_text(block.get("structured_content"))
            if result_text:
                return f"[Tool result: {name}]\n{result_text}"
            return f"[Tool result: {name}]"

        return extract_text(block)

    @staticmethod
    def _extract_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in message.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            filename = extract_text(item.get("file_name"))
            if not filename or filename in seen:
                continue
            seen.add(filename)
            attachments.append(
                {
                    "filename": filename,
                    "label": filename,
                    "url": "",
                    "file_type": extract_text(item.get("file_type")),
                    "file_size": item.get("file_size"),
                    "extracted_content": extract_text(item.get("extracted_content")),
                }
            )

        for item in message.get("files") or []:
            if not isinstance(item, dict):
                continue
            file_uuid = extract_text(item.get("file_uuid"))
            filename = extract_text(item.get("file_name")) or file_uuid
            dedupe_key = file_uuid or filename
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            attachments.append(
                {
                    "filename": filename,
                    "label": filename,
                    "url": "",
                    "file_uuid": file_uuid,
                }
            )

        return attachments

    @staticmethod
    def _build_search_text(display_content: str, attachments: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        if display_content:
            parts.append(display_content)

        for attachment in attachments:
            label = extract_text(attachment.get("label"))
            extracted_content = extract_text(attachment.get("extracted_content"))
            if label:
                parts.append(label)
            if extracted_content:
                parts.append(extracted_content)

        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _content_types(content: Any) -> list[str]:
        if not isinstance(content, list):
            return []
        types: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = extract_text(block.get("type"))
            if block_type:
                types.append(block_type)
        return types

    @staticmethod
    def _sort_messages(messages: Any) -> list[Any]:
        if not isinstance(messages, list):
            return []

        def sort_key(message: Any) -> tuple[float, str]:
            if not isinstance(message, dict):
                return (0, "")
            try:
                created_at = coerce_datetime(message.get("created_at") or datetime.fromtimestamp(0, tz=UTC))
            except Exception:
                created_at = datetime.fromtimestamp(0, tz=UTC)
            return (created_at.timestamp(), extract_text(message.get("uuid")))

        return sorted(messages, key=sort_key)
