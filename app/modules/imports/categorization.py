"""Автокатегоризация операций выписки.

1. Детерминированные правила (точные ключевые слова известных сетей) → assigned.
2. Остальное — YandexGPT Lite по уникальным мерчантам (после маскирования ПДн) →
   high → assigned, medium → suggested, low/нет ответа → unassigned.
В ИИ уходят только маскированные названия операций и названия категорий; без сумм,
дат, идентификаторов пользователя, номеров карт/счетов, телефонов и ФИО.
Работает независимо от переключателя «AI-инсайты».
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.ai.client import LLMClient, LLMInvalidResponse, LLMMessage, LLMTask, complete_json
from app.db.models import Category
from app.modules.imports.masking import mask_pii, merchant_key

log = logging.getLogger("app.imports")

Status = Literal["assigned", "suggested", "unassigned"]

# id системных категорий — из миграции 0002_system_categories.
SYS = {
    "products": uuid.UUID("00000000-0000-4000-8000-000000000001"),
    "cafe": uuid.UUID("00000000-0000-4000-8000-000000000002"),
    "transport": uuid.UUID("00000000-0000-4000-8000-000000000003"),
    "health": uuid.UUID("00000000-0000-4000-8000-000000000005"),
    "telecom": uuid.UUID("00000000-0000-4000-8000-000000000008"),
}

KEYWORD_RULES: list[tuple[tuple[str, ...], uuid.UUID]] = [
    (
        (
            "ПЯТЕРОЧКА",
            "PYATEROCHKA",
            "ПЕРЕКРЕСТОК",
            "PEREKRESTOK",
            "МАГНИТ",
            "MAGNIT",
            "ВКУСВИЛЛ",
            "VKUSVILL",
            "ЛЕНТА",
            "LENTA",
            "АШАН",
            "AUCHAN",
            "ДИКСИ",
            "DIXY",
            "САМОКАТ",
            "SAMOKAT",
        ),
        SYS["products"],
    ),
    (("АПТЕКА", "APTEKA", "РИГЛА", "RIGLA", "ГОРЗДРАВ", "GORZDRAV"), SYS["health"]),
    (("МЕТРОПОЛИТЕН", "MOSMETRO", "МОСГОРТРАНС", "MOSGORTRANS", "TROYKA", "ТРОЙКА"), SYS["transport"]),
    (("МТС", "MTS", "БИЛАЙН", "BEELINE", "МЕГАФОН", "MEGAFON", "TELE2"), SYS["telecom"]),
]


@dataclass
class CategoryGuess:
    category_id: uuid.UUID | None
    status: Status


def rule_match(key: str, active_ids: set[uuid.UUID]) -> uuid.UUID | None:
    words = set(key.replace(".", " ").split())
    for keywords, cid in KEYWORD_RULES:
        if cid in active_ids and any(k in words for k in keywords):
            return cid
    return None


class _LLMItem(BaseModel):
    i: int
    c: int | None = None
    conf: Literal["high", "medium", "low"] = "low"


class _LLMAnswer(BaseModel):
    items: list[_LLMItem] = Field(default_factory=list)


SYSTEM_PROMPT = (
    "Ты относишь банковские операции (расходы) к категориям. Тебе дан нумерованный список категорий и "
    "нумерованный список названий операций. Для каждой операции выбери номер категории или null, если "
    "не уверен. conf: high — очевидно (известная сеть, однозначное название), medium — вероятно, "
    "low — догадка. «Другое» выбирай только если операция явно не подходит ни к одной категории. "
    'Ответ — только JSON: {"items":[{"i":<номер операции>,"c":<номер категории или null>,'
    '"conf":"high|medium|low"}]}'
)


async def _ask_llm(
    client: LLMClient, keys: list[str], categories: list[Category]
) -> dict[str, CategoryGuess]:
    cat_lines = "\n".join(f"{n}. {c.name}" for n, c in enumerate(categories))
    op_lines = "\n".join(f"{n}. {k}" for n, k in enumerate(keys))
    answer = await complete_json(
        client,
        LLMTask.categorize,
        [
            LLMMessage("system", SYSTEM_PROMPT),
            LLMMessage("user", f"Категории:\n{cat_lines}\n\nОперации:\n{op_lines}"),
        ],
        _LLMAnswer,
        temperature=0.0,
        max_tokens=4000,
    )
    out: dict[str, CategoryGuess] = {}
    for item in answer.items:
        if not 0 <= item.i < len(keys):
            continue
        if item.c is None or not 0 <= item.c < len(categories) or item.conf == "low":
            out[keys[item.i]] = CategoryGuess(None, "unassigned")
        else:
            status: Status = "assigned" if item.conf == "high" else "suggested"
            out[keys[item.i]] = CategoryGuess(categories[item.c].id, status)
    return out


async def categorize(
    descriptions: list[str | None],
    categories: list[Category],
    client: LLMClient | None,
    batch_size: int,
) -> list[CategoryGuess]:
    """Возвращает предположение для каждой операции (в том же порядке).

    LLMError (недоступность/таймаут) пробрасывается — по контракту это 500 processing_failed.
    Невалидный ответ модели не роняет парсинг: операции батча остаются unassigned.
    """
    active_ids = {c.id for c in categories}
    keys = [merchant_key(mask_pii(d)) for d in descriptions]
    by_key: dict[str, CategoryGuess] = {}
    pending: list[str] = []
    for k in dict.fromkeys(keys):
        if not k:
            by_key[k] = CategoryGuess(None, "unassigned")
        elif (cid := rule_match(k, active_ids)) is not None:
            by_key[k] = CategoryGuess(cid, "assigned")
        else:
            pending.append(k)

    if pending and client is not None and categories:
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            try:
                by_key.update(await _ask_llm(client, batch, categories))
            except LLMInvalidResponse:
                log.warning("categorization_invalid_response", extra={"count": len(batch)})

    return [by_key.get(k, CategoryGuess(None, "unassigned")) for k in keys]
