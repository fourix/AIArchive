from __future__ import annotations

import html
import hashlib
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        raw = " ".join(self.parts)
        normalized = re.sub(r"\s+", " ", raw).strip()
        return html.unescape(normalized)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def ensure_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("conversations", "chats", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    raise ValueError("JSON payload must be a list or object")


def coerce_datetime(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, dict):
        if "$date" in value:
            return coerce_datetime(value["$date"], fallback=fallback)
        if "$numberLong" in value:
            number_long = str(value["$numberLong"]).strip()
            if number_long:
                return datetime.fromtimestamp(int(number_long) / 1000, tz=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            timestamp = int(stripped)
            if len(stripped) >= 13:
                return datetime.fromtimestamp(timestamp / 1000, tz=UTC)
            return datetime.fromtimestamp(timestamp, tz=UTC)
        normalized = stripped.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if fallback is not None:
        return fallback
    raise ValueError(f"Unsupported datetime value: {value!r}")


def stable_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        chunks = [extract_text(item) for item in value]
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "value", "message"):
            if key in value:
                text = extract_text(value[key])
                if text:
                    return text
        if "parts" in value:
            return extract_text(value["parts"])
        if "chunks" in value:
            return extract_text(value["chunks"])
        return json_dumps(value)
    return str(value).strip()


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.get_text()
