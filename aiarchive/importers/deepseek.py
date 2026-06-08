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
        node_by_id: dict[str, dict[str, Any]] = {}
        for fallback_id, node in mapping.items():
            if not isinstance(node, dict):
                continue
            node_id = extract_text(node.get("id")) or str(fallback_id)
            if not node_id:
                continue
            node_by_id[node_id] = node

        ordered: list[dict[str, Any]] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            node = node_by_id.get(node_id)
            if node is None:
                return

            visited.add(node_id)
            if isinstance(node.get("message"), dict):
                ordered.append(node)

            for child_id in node.get("children") or []:
                visit(str(child_id))

        root_ids = ["root"] if "root" in node_by_id else []
        root_ids.extend(
            node_id
            for node_id, node in node_by_id.items()
            if node_id not in root_ids and str(node.get("parent") or "") not in node_by_id
        )

        for node_id in root_ids:
            visit(node_id)

        for node_id in sorted(node_by_id, key=DeepSeekImporter._node_id_sort_key):
            visit(node_id)

        return ordered

    @staticmethod
    def _node_id_sort_key(value: str) -> tuple[int, int | str]:
        return (0, int(value)) if value.isdigit() else (1, value)

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
