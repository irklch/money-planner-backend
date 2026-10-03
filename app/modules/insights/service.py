"""GET /insights — AI-выводы по агрегатам.

Математику считает backend (analytics.service). ИИ получает только готовые факты
(названия категорий, суммы, доли, изменения) и формулирует выводы. Comment расходов,
идентификаторы пользователя и сырые операции в ИИ не передаются.
Каждый вывод проверяется: все числа в тексте должны совпадать с числами из фактов.
"""

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import LLMClient, LLMInvalidResponse, LLMMessage, LLMTask, complete_json
from app.modules.analytics import service as analytics
from app.modules.categories.service import list_categories

log = logging.getLogger("app.insights")

MAX_ITEMS = 3
TOP_CATEGORIES = 6
_NUM_RE = re.compile(r"\d[\d   ]*(?:[.,]\d+)?")


def fmt_rub(d: Decimal) -> str:
    n = int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{n:,}".replace(",", " ")


def fmt_pct(x: float) -> str:
    return str(int(round(abs(x))))


def extract_numbers(text: str) -> set[str]:
    out = set()
    for m in _NUM_RE.findall(text):
        s = re.sub(r"[\s  ]", "", m).replace(",", ".").rstrip(".")
        if s:
            out.add(s.lstrip("0") or "0")
    return out


@dataclass
class Fact:
    text: str
    category_index: int | None = None


@dataclass
class InsightItem:
    id: str
    title: str
    detail: str
    target_type: Literal["category", "period"]
    category_id: uuid.UUID | None


class _LLMInsight(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=240)
    target: Literal["category", "period"]
    category: int | None = None


class _LLMAnswer(BaseModel):
    items: list[_LLMInsight] = Field(default_factory=list, max_length=MAX_ITEMS + 2)


SYSTEM_PROMPT = (
    "Ты помощник по личным финансам. По готовым фактам о расходах сформулируй до 3 коротких наблюдений "
    "на русском. Используй ТОЛЬКО числа, которые есть в фактах, без изменений; ничего не вычисляй сам "
    "и не придумывай новых чисел. Без советов по инвестициям, без оценок и морализаторства. "
    "title — до 60 символов, detail — до 200 символов. "
    "target: category (наблюдение про одну категорию — укажи её номер в category) или period. "
    'Ответ — только JSON: {"items":[{"title":"...","detail":"...",'
    '"target":"category|period","category":<номер|null>}]}'
)


async def build_facts(
    session: AsyncSession, user_id: uuid.UUID, start: date, end: date, compare: tuple[date, date] | None
) -> tuple[list[Fact], list[uuid.UUID]]:
    s = await analytics.summary(session, user_id, start, end, compare)
    if s.total <= 0:
        return [], []
    names = {c.id: c.name for c in await list_categories(session, user_id, include_archived=True)}
    facts = [Fact(f"Период: {start.isoformat()} — {end.isoformat()}. Всего потрачено: {fmt_rub(s.total)} ₽.")]
    if s.previous_total is not None:
        line = f"В периоде сравнения потрачено: {fmt_rub(s.previous_total)} ₽."
        if s.previous_total > 0:
            change = float((s.total - s.previous_total) / s.previous_total * 100)
            direction = "больше" if change >= 0 else "меньше"
            diff = fmt_rub(abs(s.total - s.previous_total))
            line += f" Это на {fmt_pct(change)}% {direction}, разница {diff} ₽."
        facts.append(Fact(line))
    refs: list[uuid.UUID] = []
    for c in s.categories[:TOP_CATEGORIES]:
        idx = len(refs)
        refs.append(c.category_id)
        line = (
            f"Категория №{idx} «{names.get(c.category_id, 'Без названия')}»: {fmt_rub(c.total)} ₽, "
            f"{fmt_pct(c.share * 100)}% всех расходов, операций: {c.expense_count}."
        )
        if c.delta is not None and c.delta_percent is not None:
            direction = "больше" if c.delta >= 0 else "меньше"
            line += (
                f" К периоду сравнения: на {fmt_pct(c.delta_percent)}% {direction} "
                f"({fmt_rub(abs(c.delta))} ₽)."
            )
        facts.append(Fact(line, category_index=idx))
    return facts, refs


def validate_item(item: _LLMInsight, allowed: set[str], refs: list[uuid.UUID]) -> InsightItem | None:
    text = f"{item.title} {item.detail}"
    if not extract_numbers(text) <= allowed:
        return None
    category_id = None
    if item.target == "category":
        if item.category is None or not 0 <= item.category < len(refs):
            return None
        category_id = refs[item.category]
    digest = hashlib.sha256(f"{item.target}:{category_id}:{item.title}".encode()).hexdigest()[:16]
    return InsightItem(
        id=f"ins_{digest}",
        title=item.title.strip(),
        detail=item.detail.strip(),
        target_type=item.target,
        category_id=category_id,
    )


async def generate(
    session: AsyncSession,
    client: LLMClient,
    user_id: uuid.UUID,
    start: date,
    end: date,
    compare: tuple[date, date] | None,
) -> list[InsightItem]:
    facts, refs = await build_facts(session, user_id, start, end, compare)
    if not facts:
        return []
    facts_text = "\n".join(f.text for f in facts)
    allowed = extract_numbers(facts_text)
    try:
        answer = await complete_json(
            client,
            LLMTask.insights,
            [LLMMessage("system", SYSTEM_PROMPT), LLMMessage("user", f"Факты:\n{facts_text}")],
            _LLMAnswer,
            temperature=0.3,
            max_tokens=800,
        )
    except LLMInvalidResponse:
        log.warning("insights_invalid_response")
        return []
    items = []
    for raw in answer.items:
        if (item := validate_item(raw, allowed, refs)) is not None:
            items.append(item)
        else:
            log.warning("insight_rejected", extra={"reason": "numbers_or_target_mismatch"})
    return items[:MAX_ITEMS]
