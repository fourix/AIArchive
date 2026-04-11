from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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


settings = Settings()
