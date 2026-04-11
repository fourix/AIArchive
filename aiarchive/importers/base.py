from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..models import NormalizedConversation


class BaseImporter(ABC):
    platform: str

    def parse_bytes(self, raw: bytes, source_path: Path | None = None) -> list[NormalizedConversation]:
        payload = self._load_json_bytes(raw)
        return self.parse_payload(payload, source_path=source_path)

    @staticmethod
    def _load_json_bytes(raw: bytes) -> Any:
        encodings = (
            "utf-8",
            "utf-8-sig",
            "utf-16",
            "utf-16-le",
            "utf-16-be",
            "utf-32",
            "utf-32-le",
            "utf-32-be",
        )
        last_error: Exception | None = None

        for encoding in encodings:
            try:
                return json.loads(raw.decode(encoding))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                last_error = exc

        raise ValueError(
            "Could not decode JSON file. Supported encodings tried: "
            + ", ".join(encodings)
        ) from last_error

    @abstractmethod
    def parse_payload(self, payload: Any, source_path: Path | None = None) -> list[NormalizedConversation]:
        raise NotImplementedError
