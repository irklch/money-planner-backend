"""Суточные лимиты частоты в PostgreSQL (`rate_counters`, план §9): минутные — в памяти процесса
(`rate_limit.py`), суточные переживают перезапуск `api`.

Окно — сутки UTC, `window_start` — их начало. Учёт атомарный: одна команда
`INSERT … ON CONFLICT DO UPDATE … WHERE hits < limit`, поэтому параллельные запросы не превышают лимит
и не теряют попадания. Строки удаляет `maintenance` по `expires_at` (E8).

Механизм — E2; применяет его E3 (`IMPORT_RATE_PER_DAY` для разбора выписок).
"""

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.schema import RateCounter

DAY = timedelta(days=1)


def day_window(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + DAY


async def hit_daily(session: AsyncSession, key: str, limit: int, now: datetime) -> int:
    """Засчитать запрос в суточном окне. Возвращает число запросов в окне с этим; сверх лимита —
    `429 rate_limited` с Retry-After до начала следующих суток (попадание не засчитывается).
    Фиксирует транзакцию сессии."""
    start, end = day_window(now)
    stmt = (
        insert(RateCounter)
        .values(counter_key=key, window_start=start, hits=1, expires_at=end)
        .on_conflict_do_update(
            index_elements=[RateCounter.counter_key, RateCounter.window_start],
            set_={"hits": RateCounter.hits + 1},
            where=RateCounter.hits < limit,
        )
        .returning(RateCounter.hits)
    )
    hits = await session.scalar(stmt)
    await session.commit()
    if hits is None or hits > limit:
        raise ApiError("rate_limited", retry_after=max(1, math.ceil((end - now).total_seconds())))
    return hits
