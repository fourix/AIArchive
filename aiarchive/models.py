from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class NormalizedMessage:
    source_message_id: str
    role: str
    content: str
    created_at: datetime
    sequence_no: int
    metadata_json: str = "{}"


@dataclass(slots=True)
class NormalizedConversation:
    platform: str
    source_conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    metadata_json: str = "{}"
    messages: list[NormalizedMessage] = field(default_factory=list)
