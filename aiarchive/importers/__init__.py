from .base import BaseImporter
from .deepseek import DeepSeekImporter
from .gemini import GeminiImporter
from .grok import GrokImporter
from .openai import OpenAIImporter

IMPORTERS: dict[str, type[BaseImporter]] = {
    "openai": OpenAIImporter,
    "gemini": GeminiImporter,
    "grok": GrokImporter,
    "deepseek": DeepSeekImporter,
}


def get_importer(platform: str) -> BaseImporter:
    try:
        importer_cls = IMPORTERS[platform.lower()]
    except KeyError as exc:
        supported = ", ".join(sorted(IMPORTERS))
        raise ValueError(f"Unsupported platform '{platform}'. Supported: {supported}") from exc
    return importer_cls()
