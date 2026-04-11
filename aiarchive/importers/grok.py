from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ..models import NormalizedConversation, NormalizedMessage
from .base import BaseImporter
from .common import coerce_datetime, ensure_list, extract_text, json_dumps, stable_hash


class GrokImporter(BaseImporter):
    platform = "grok"

    def parse_payload(self, payload: Any, source_path: Any = None) -> list[NormalizedConversation]:
        conversations: list[NormalizedConversation] = []
        for item in ensure_list(payload):
            if not isinstance(item, dict):
                continue
            conversations.append(self._parse_conversation(item))
        return conversations

    def _parse_conversation(self, item: dict[str, Any]) -> NormalizedConversation:
        conversation_info = item.get("conversation") if isinstance(item.get("conversation"), dict) else item
        responses = item.get("responses") or item.get("messages") or item.get("items") or []

        conversation_id = str(
            conversation_info.get("id")
            or conversation_info.get("conversation_id")
            or conversation_info.get("chat_id")
            or stable_hash(json_dumps(item))
        )
        title = self._normalize_title(conversation_info.get("title") or conversation_info.get("name") or "")
        created_at = coerce_datetime(
            conversation_info.get("create_time")
            or conversation_info.get("created_at")
            or conversation_info.get("timestamp")
            or datetime.now(UTC)
        )
        updated_at = coerce_datetime(
            conversation_info.get("modify_time")
            or conversation_info.get("updated_at")
            or conversation_info.get("timestamp")
            or created_at,
            fallback=created_at,
        )

        messages: list[NormalizedMessage] = []
        for sequence_no, raw_entry in enumerate(responses, start=1):
            response = raw_entry.get("response") if isinstance(raw_entry, dict) and isinstance(raw_entry.get("response"), dict) else raw_entry
            if not isinstance(response, dict):
                continue

            attachments = self._extract_attachments(response)
            content = self._sanitize_message_text(
                extract_text(response.get("message") or response.get("content") or response.get("text") or "")
            )
            if not content and not attachments:
                continue

            role = self._map_role(response.get("sender"))
            message_created = coerce_datetime(
                response.get("create_time")
                or response.get("created_at")
                or response.get("timestamp")
                or created_at,
                fallback=created_at,
            )
            message_id = str(
                response.get("_id")
                or response.get("id")
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
                            "raw_keys": sorted(response.keys()),
                            "attachments": attachments,
                            "model": response.get("model"),
                            "request_model": self._extract_request_model(response.get("metadata")),
                            "parent_response_id": response.get("parent_response_id"),
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
                    "export_format": "grok_backend",
                    "original_title": conversation_info.get("title"),
                    "temporary": conversation_info.get("temporary"),
                    "starred": conversation_info.get("starred"),
                }
            ),
            messages=messages,
        )

    @staticmethod
    def _normalize_title(value: Any) -> str:
        title = extract_text(value)
        return title or "Untitled conversation"

    @staticmethod
    def _sanitize_message_text(value: str) -> str:
        if not value:
            return ""
        cleaned = re.sub(r"<grok:render\b[^>]*>.*?</grok:render>", "", value, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _map_role(sender: Any) -> str:
        normalized = str(sender or "").strip().lower()
        if normalized in {"human", "user"}:
            return "user"
        if normalized in {"assistant", "grok", "model"}:
            return "assistant"
        return normalized or "unknown"

    @staticmethod
    def _extract_request_model(metadata: Any) -> str:
        if not isinstance(metadata, dict):
            return ""
        request_model = metadata.get("requestModelDetails")
        if isinstance(request_model, dict):
            model_id = extract_text(request_model.get("modelId"))
            if model_id:
                return model_id
        request_metadata = metadata.get("request_metadata")
        if isinstance(request_metadata, dict):
            model_name = extract_text(request_metadata.get("model"))
            if model_name:
                return model_name
        return ""

    @staticmethod
    def _extract_attachments(response: dict[str, Any]) -> list[dict[str, str]]:
        attachments: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in response.get("file_attachments") or []:
            attachment_id = extract_text(item)
            if not attachment_id or attachment_id in seen:
                continue
            seen.add(attachment_id)
            attachments.append(
                {
                    "filename": attachment_id,
                    "label": attachment_id,
                    "url": "",
                }
            )
        return attachments
