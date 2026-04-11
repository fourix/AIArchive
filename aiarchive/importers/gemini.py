from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..models import NormalizedConversation, NormalizedMessage
from .base import BaseImporter
from .common import coerce_datetime, ensure_list, extract_text, html_to_text, json_dumps, stable_hash


class GeminiImporter(BaseImporter):
    platform = "gemini"

    def parse_payload(self, payload: Any, source_path: Any = None) -> list[NormalizedConversation]:
        conversations: list[NormalizedConversation] = []
        for item in ensure_list(payload):
            conversations.append(self._parse_record(item))
        return conversations

    def _parse_record(self, item: dict[str, Any]) -> NormalizedConversation:
        created_at = coerce_datetime(
            item.get("time") or item.get("created_at") or item.get("create_time") or datetime.now(UTC)
        )
        user_attachments = self._extract_user_attachments(item)
        raw_title = self._normalize_prompt_title(item.get("title") or item.get("name") or "")
        has_user_text = bool(raw_title)
        title = raw_title or "<No Title>"
        searchable_user_content = self._build_user_search_text(raw_title, user_attachments)
        assistant_html = self._extract_safe_html_raw(item.get("safeHtmlItem") or [])
        assistant_content = self._extract_safe_html_text(item.get("safeHtmlItem") or [])
        record_id = self._build_record_id(
            item=item,
            created_at=created_at,
            title=title,
            assistant_html=assistant_html,
            user_attachments=user_attachments,
        )
        messages: list[NormalizedMessage] = [
            NormalizedMessage(
                source_message_id=stable_hash(record_id, "user", searchable_user_content, created_at.isoformat()),
                role="user",
                content=searchable_user_content,
                created_at=created_at,
                sequence_no=1,
                metadata_json=json_dumps(
                    {
                        "source": "gemini_title",
                        "display_content": raw_title if has_user_text else "",
                        "attachments": user_attachments,
                    }
                ),
            ),
            NormalizedMessage(
                source_message_id=stable_hash(record_id, "assistant", assistant_content, created_at.isoformat()),
                role="assistant",
                content=assistant_content,
                created_at=created_at,
                sequence_no=2,
                metadata_json=json_dumps(
                    {
                        "source": "gemini_safe_html",
                        "html_content": assistant_html,
                    }
                ),
            ),
        ]

        return NormalizedConversation(
            platform=self.platform,
            source_conversation_id=record_id,
            title=title,
            created_at=created_at,
            updated_at=created_at,
            metadata_json=json_dumps(
                {
                    "export_format": "gemini",
                    "original_id": extract_text(item.get("id")),
                    "original_conversation_id": extract_text(item.get("conversation_id")),
                    "dedupe_basis": {
                        "time": created_at.isoformat(),
                        "title": title,
                        "attachment_names": [attachment.get("filename", "") for attachment in user_attachments],
                        "assistant_html_hash": stable_hash(assistant_html),
                    },
                    "header": item.get("header"),
                    "products": item.get("products"),
                    "activity_controls": item.get("activityControls"),
                }
            ),
            messages=messages,
        )

    @staticmethod
    def _build_record_id(
        item: dict[str, Any],
        created_at: datetime,
        title: str,
        assistant_html: str,
        user_attachments: list[dict[str, str]],
    ) -> str:
        source_id = extract_text(item.get("id"))
        conversation_id = extract_text(item.get("conversation_id"))
        header = extract_text(item.get("header"))
        products = "|".join(sorted(extract_text(value) for value in item.get("products") or [] if extract_text(value)))
        activity_controls = "|".join(
            sorted(extract_text(value) for value in item.get("activityControls") or [] if extract_text(value))
        )
        attachment_signature = "|".join(
            sorted(
                f"{attachment.get('filename', '').strip()}::{attachment.get('label', '').strip()}"
                for attachment in user_attachments
                if isinstance(attachment, dict)
            )
        )

        return stable_hash(
            "gemini_record",
            source_id,
            conversation_id,
            created_at.isoformat(),
            title.strip(),
            stable_hash(assistant_html.strip()),
            attachment_signature,
            header,
            products,
            activity_controls,
        )

    @staticmethod
    def _normalize_prompt_title(value: Any) -> str:
        title = extract_text(value)
        if title == "Prompted":
            return ""
        if title.startswith("Prompted "):
            title = title[len("Prompted ") :].strip()
        return title.strip()

    @staticmethod
    def _build_user_search_text(raw_title: str, user_attachments: list[dict[str, str]]) -> str:
        parts: list[str] = []
        if raw_title:
            parts.append(raw_title)

        for attachment in user_attachments:
            if not isinstance(attachment, dict):
                continue
            label = extract_text(attachment.get("label"))
            filename = extract_text(attachment.get("filename"))
            if label:
                parts.append(label)
            elif filename:
                parts.append(filename)

        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _extract_user_attachments(item: dict[str, Any]) -> list[dict[str, str]]:
        attachments: list[dict[str, str]] = []
        seen: set[str] = set()

        subtitles = item.get("subtitles") or []
        subtitle_names: dict[str, str] = {}
        for entry in subtitles:
            if not isinstance(entry, dict):
                continue
            url = extract_text(entry.get("url"))
            name = extract_text(entry.get("name"))
            if url:
                cleaned_name = name.lstrip("-").strip() if name else ""
                subtitle_names[url] = cleaned_name or url

        for filename in item.get("attachedFiles") or []:
            normalized = extract_text(filename)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            attachments.append(
                {
                    "filename": normalized,
                    "label": subtitle_names.get(normalized, normalized),
                }
            )

        image_file = extract_text(item.get("imageFile"))
        if image_file and image_file not in seen:
            seen.add(image_file)
            attachments.append(
                {
                    "filename": image_file,
                    "label": subtitle_names.get(image_file, image_file),
                }
            )

        return attachments

    @staticmethod
    def _extract_safe_html_text(items: list[Any]) -> str:
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            html_value = item.get("html")
            if isinstance(html_value, str) and html_value.strip():
                text = html_to_text(html_value)
                if text:
                    parts.append(text)
        content = "\n\n".join(parts).strip()
        return content or "[No assistant content]"

    @staticmethod
    def _extract_safe_html_raw(items: list[Any]) -> str:
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            html_value = item.get("html")
            if isinstance(html_value, str) and html_value.strip():
                parts.append(html_value)
        content = "\n\n".join(parts).strip()
        return content or "<p>[No assistant content]</p>"
