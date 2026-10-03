from functools import lru_cache

from app.ai.client import LLMClient
from app.core.config import get_settings

_override: LLMClient | None = None


def set_llm_client(client: LLMClient | None) -> None:
    """Подмена клиента (тесты, будущий резервный провайдер)."""
    global _override
    _override = client
    _default.cache_clear()


@lru_cache
def _default() -> LLMClient | None:
    settings = get_settings()
    if not settings.ai_enabled:
        return None
    from app.ai.yandex import YandexLLMClient

    return YandexLLMClient(settings)


def get_llm_client() -> LLMClient | None:
    """None — ИИ выключен конфигурацией."""
    return _override if _override is not None else _default()
