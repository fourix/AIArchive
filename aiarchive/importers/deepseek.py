from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..models import NormalizedConversation, NormalizedMessage
from .base import BaseImporter
from .common import coerce_datetime, ensure_list, extract_text, json_dumps, stable_hash


class DeepSeekImporter(BaseImporter):
    platform = "deepseek"

    def parse_payload(self, payload: Any, source_path: Any = None) -> list[NormalizedConversation]:
        conversations: list[NormalizedConversation] = []
        for item in ensure_list(payload):
            conversations.append(self._parse_conversation(item))
        return conversations

    def _parse_conversation(self, item: dict[str, Any]) -> NormalizedConversation:
        conversation_id = str(item.get("id") or item.get("session_id") or stable_hash(json_dumps(item)))
        title = (item.get("title") or item.get("topic") or "Untitled conversation").strip()
        created_at = coerce_datetime(
            item.get("inserted_at") or item.get("created_at") or item.get("timestamp") or datetime.now(UTC)
        )
        updated_at = coerce_datetime(item.get("updated_at") or item.get("timestamp") or created_at, fallback=created_at)

        messages: list[NormalizedMessage] = []
        mapping = item.get("mapping")
        if isinstance(mapping, dict):
            raw_messages = self._sorted_nodes(mapping)
        else:
            raw_messages = item.get("messages") or item.get("history") or []

        for sequence_no, raw_message in enumerate(raw_messages, start=1):
            message = raw_message.get("message") if "message" in raw_message else raw_message
            if not isinstance(message, dict):
                continue

            fragments = message.get("fragments") or []
            content = self._extract_fragments_text(fragments)
            if not content:
                continue

            role = self._derive_role(message, fragments)
            message_created = coerce_datetime(
                message.get("inserted_at")
                or raw_message.get("inserted_at")
                or message.get("created_at")
                or raw_message.get("created_at")
                or raw_message.get("timestamp")
                or created_at,
                fallback=created_at,
            )
            message_id = str(
                raw_message.get("id")
                or message.get("id")
                or stable_hash(conversation_id, str(sequence_no), role, content, message_created.isoformat())
            )
            messages.append(
                NormalizedMessage(
                    source_message_id=message_id,
                    role=role,
                    content=content,
                    created_at=message_created,
                    sequence_no=sequence_no,
                    metadata_json=json_dumps(
                        {
                            "raw_keys": sorted(raw_message.keys()),
                            "model": message.get("model"),
                            "fragment_types": [fragment.get("type") for fragment in fragments if isinstance(fragment, dict)],
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
            metadata_json=json_dumps({"export_format": "deepseek", "raw_title": item.get("title")}),
            messages=messages,
        )

    @staticmethod
    def _sorted_nodes(mapping: dict[str, Any]) -> list[dict[str, Any]]:
        nodes: list[tuple[str, dict[str, Any]]] = []
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            sort_value = (
                message.get("inserted_at")
                or node.get("inserted_at")
                or message.get("created_at")
                or node.get("created_at")
                or ""
            )
            nodes.append((str(sort_value), node))
        nodes.sort(key=lambda item: item[0])
        return [node for _, node in nodes]

    @staticmethod
    def _extract_fragments_text(fragments: list[Any]) -> str:
        parts: list[str] = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            text = extract_text(fragment.get("content"))
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _derive_role(message: dict[str, Any], fragments: list[Any]) -> str:
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            fragment_type = str(fragment.get("type") or "").upper()
            if fragment_type == "REQUEST":
                return "user"
            if fragment_type == "RESPONSE":
                return "assistant"
        return str(message.get("role") or message.get("author") or "unknown")
