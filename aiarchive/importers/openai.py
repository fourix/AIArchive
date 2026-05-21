from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..models import NormalizedConversation, NormalizedMessage
from .base import BaseImporter
from .common import coerce_datetime, ensure_list, extract_text, json_dumps, stable_hash


class OpenAIImporter(BaseImporter):
    platform = "openai"

    def parse_payload(self, payload: Any, source_path: Any = None) -> list[NormalizedConversation]:
        conversations: list[NormalizedConversation] = []
        for item in ensure_list(payload):
            if not isinstance(item, dict):
                continue
            conversations.append(self._parse_conversation(item))
        return conversations

    def _parse_conversation(self, item: dict[str, Any]) -> NormalizedConversation:
        conversation_id = str(item.get("id") or item.get("conversation_id") or stable_hash(json_dumps(item)))
        created_at = coerce_datetime(item.get("create_time") or item.get("created_at") or datetime.now(UTC))
        updated_at = coerce_datetime(item.get("update_time") or item.get("updated_at") or created_at, fallback=created_at)
        title = self._normalize_title(item.get("title"))

        messages: list[NormalizedMessage] = []
        mapping = item.get("mapping") or {}
        for node in self._sorted_nodes(mapping):
            message = node.get("message") or {}
            if not isinstance(message, dict) or not message:
                continue

            author = message.get("author") or {}
            role = self._map_role(author.get("role"))
            if role not in {"user", "assistant"}:
                continue

            display_content, searchable_content, attachments, content_type = self._extract_message_payload(message)
            if not searchable_content and not attachments:
                continue

            message_created = coerce_datetime(
                message.get("create_time") or node.get("create_time") or created_at,
                fallback=created_at,
            )
            message_id = str(
                message.get("id")
                or node.get("id")
                or stable_hash(conversation_id, str(len(messages) + 1), role, searchable_content, message_created.isoformat())
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
                            "node_id": node.get("id"),
                            "raw_author": author,
                            "content_type": content_type,
                            "display_content": display_content,
                            "attachments": attachments,
                            "model_slug": self._extract_model_slug(item, message),
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
                    "export_format": "openai",
                    "raw_title": item.get("title"),
                    "conversation_template_id": item.get("conversation_template_id"),
                    "default_model_slug": item.get("default_model_slug"),
                    "is_archived": item.get("is_archived"),
                    "is_temporary_chat": item.get("is_temporary_chat"),
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
        if normalized == "assistant":
            return "assistant"
        if normalized == "user":
            return "user"
        return normalized

    def _extract_message_payload(
        self,
        message: dict[str, Any],
    ) -> tuple[str, str, list[dict[str, str]], str]:
        content = message.get("content") or {}
        metadata = message.get("metadata") or {}
        content_type = extract_text(content.get("content_type")) if isinstance(content, dict) else type(content).__name__

        attachments = self._extract_attachments(content, metadata)
        display_content = self._extract_display_text(content)
        searchable_content = self._build_search_text(display_content, attachments)
        return display_content, searchable_content, attachments, content_type

    def _extract_display_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()

        if not isinstance(content, dict):
            return extract_text(content)

        text_value = extract_text(content.get("text"))
        if text_value:
            return text_value

        if "parts" in content:
            return self._extract_parts_text(content.get("parts"))

        for key in ("result", "output", "value", "message"):
            candidate = extract_text(content.get(key))
            if candidate:
                return candidate

        return ""

    def _extract_parts_text(self, value: Any) -> str:
        parts: list[str] = []

        def visit(item: Any) -> None:
            if item is None:
                return
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    parts.append(cleaned)
                return
            if isinstance(item, list):
                for nested in item:
                    visit(nested)
                return
            if not isinstance(item, dict):
                cleaned = extract_text(item)
                if cleaned:
                    parts.append(cleaned)
                return

            if item.get("asset_pointer"):
                return

            for key in ("text", "content", "value", "message", "title"):
                candidate = extract_text(item.get(key))
                if candidate:
                    parts.append(candidate)
                    return

            nested_parts = item.get("parts")
            if nested_parts is not None:
                visit(nested_parts)

        visit(value)
        return "\n".join(part for part in parts if part).strip()

    def _build_search_text(self, display_content: str, attachments: list[dict[str, str]]) -> str:
        parts: list[str] = []
        if display_content:
            parts.append(display_content)

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            label = extract_text(attachment.get("label"))
            filename = extract_text(attachment.get("filename"))
            source_id = extract_text(attachment.get("source_id"))
            if label:
                parts.append(label)
            elif filename:
                parts.append(filename)
            elif source_id:
                parts.append(source_id)

        return "\n".join(part for part in parts if part).strip()

    def _extract_attachments(self, content: Any, metadata: Any) -> list[dict[str, str]]:
        attachments: list[dict[str, str]] = []
        seen: set[str] = set()

        if isinstance(metadata, dict):
            for entry in metadata.get("attachments") or []:
                attachment = self._normalize_attachment_entry(entry)
                source_id = attachment.get("source_id", "")
                if not source_id or source_id in seen:
                    continue
                seen.add(source_id)
                attachments.append(attachment)

        parts = content.get("parts") if isinstance(content, dict) else None
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                raw_pointer = extract_text(part.get("asset_pointer"))
                if not raw_pointer:
                    continue
                source_id = raw_pointer.removeprefix("file-service://")
                if not source_id or source_id in seen:
                    continue
                seen.add(source_id)
                attachments.append(
                    {
                        "filename": source_id,
                        "label": source_id,
                        "url": "",
                        "source_id": source_id,
                        "mime_type": "",
                    }
                )

        return attachments

    @staticmethod
    def _normalize_attachment_entry(entry: Any) -> dict[str, str]:
        if not isinstance(entry, dict):
            return {}

        source_id = extract_text(entry.get("id") or entry.get("file_id") or entry.get("asset_id"))
        label = extract_text(entry.get("name") or entry.get("filename") or source_id)
        mime_type = extract_text(entry.get("mimeType") or entry.get("mime_type"))

        if not source_id:
            return {}

        return {
            "filename": source_id,
            "label": label or source_id,
            "url": "",
            "source_id": source_id,
            "mime_type": mime_type,
        }

    @staticmethod
    def _extract_model_slug(conversation: dict[str, Any], message: dict[str, Any]) -> str:
        metadata = message.get("metadata") or {}
        if isinstance(metadata, dict):
            model_slug = extract_text(metadata.get("model_slug"))
            if model_slug:
                return model_slug
        return extract_text(conversation.get("default_model_slug"))

    @staticmethod
    def _sorted_nodes(mapping: dict[str, Any]) -> list[dict[str, Any]]:
        nodes: list[tuple[float, str, dict[str, Any]]] = []
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            message = node.get("message") or {}
            if not isinstance(message, dict) or not message:
                continue
            created_value = message.get("create_time") or node.get("create_time") or 0
            try:
                created_at = coerce_datetime(created_value, fallback=datetime.fromtimestamp(0, tz=UTC))
            except Exception:
                created_at = datetime.fromtimestamp(0, tz=UTC)
            sort_id = str(message.get("id") or node.get("id") or "")
            nodes.append((created_at.timestamp(), sort_id, node))
        nodes.sort(key=lambda item: (item[0], item[1]))
        return [node for _, _, node in nodes]
