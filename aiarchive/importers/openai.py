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
            conversations.append(self._parse_conversation(item))
        return conversations

    def _parse_conversation(self, item: dict[str, Any]) -> NormalizedConversation:
        conversation_id = str(item.get("id") or stable_hash(json_dumps(item)))
        created_at = coerce_datetime(item.get("create_time") or item.get("created_at") or datetime.now(UTC))
        updated_at = coerce_datetime(item.get("update_time") or item.get("updated_at") or created_at, fallback=created_at)
        title = (item.get("title") or "Untitled conversation").strip()

        messages: list[NormalizedMessage] = []
        mapping = item.get("mapping") or {}
        for sequence_no, node in enumerate(self._sorted_nodes(mapping), start=1):
            message = node.get("message") or {}
            content = extract_text((message.get("content") or {}).get("parts") or message.get("content"))
            if not content:
                continue
            author = message.get("author") or {}
            role = str(author.get("role") or "unknown")
            message_id = str(message.get("id") or node.get("id") or stable_hash(conversation_id, str(sequence_no), role, content))
            message_created = coerce_datetime(message.get("create_time") or node.get("create_time") or created_at, fallback=created_at)
            messages.append(
                NormalizedMessage(
                    source_message_id=message_id,
                    role=role,
                    content=content,
                    created_at=message_created,
                    sequence_no=sequence_no,
                    metadata_json=json_dumps({"node_id": node.get("id"), "raw_author": author}),
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
            metadata_json=json_dumps({"export_format": "openai", "raw_title": item.get("title")}),
            messages=messages,
        )

    @staticmethod
    def _sorted_nodes(mapping: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = []
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            message = node.get("message") or {}
            if not message:
                continue
            create_time = message.get("create_time") or node.get("create_time") or 0
            nodes.append((create_time, node))
        nodes.sort(key=lambda item: item[0] or 0)
        return [node for _, node in nodes]
