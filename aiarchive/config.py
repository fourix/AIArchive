from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _normalize_root_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw == "/":
        return ""
    return "/" + raw.strip("/")


@dataclass(frozen=True)
class Settings:
    base_dir: Path = Path(__file__).resolve().parent.parent
    data_dir: Path = base_dir / "data"
    imports_dir: Path = base_dir / "imports"
    media_dir: Path = data_dir / "media"
    database_path: Path = data_dir / "archive.db"
    templates_dir: Path = Path(__file__).resolve().parent / "templates"
    static_dir: Path = Path(__file__).resolve().parent / "static"
    app_title: str = "AI Chat Archive"
    root_path: str = field(default_factory=lambda: _normalize_root_path(os.getenv("AIARCHIVE_ROOT_PATH", "")))


settings = Settings()
