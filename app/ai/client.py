"""Собственный интерфейс LLM. Особенности провайдеров живут только в app/ai/*.

Модули приложения используют только LLMClient и Pydantic-модели ответов. Сырые ответы модели
всегда валидируются; невалидный ответ — LLMInvalidResponse, а не исключение провайдера.
"""

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ValidationError


class LLMTask(StrEnum):
    categorize = "categorize"  # YandexGPT Lite
    insights = "insights"  # YandexGPT Pro


@dataclass(frozen=True)
class LLMMessage:
    role: str  # system | user
    text: str


class LLMError(Exception):
    """Провайдер недоступен, таймаут, 5xx."""


class LLMInvalidResponse(LLMError):
    """Ответ получен, но не прошёл валидацию."""


class LLMClient(Protocol):
    async def complete(
        self, task: LLMTask, messages: list[LLMMessage], *, temperature: float = 0.1, max_tokens: int = 2000
    ) -> str: ...

    async def verify(self) -> None:
        """Проверка подключения и идентификаторов моделей (вызывается при старте/из CLI)."""
        ...


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


async def complete_json[T: BaseModel](
    client: LLMClient, task: LLMTask, messages: list[LLMMessage], model: type[T], **kw
) -> T:
    text = await client.complete(task, messages, **kw)
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return model.model_validate(json.loads(cleaned))
    except (json.JSONDecodeError, ValidationError) as e:
        raise LLMInvalidResponse(type(e).__name__) from None
