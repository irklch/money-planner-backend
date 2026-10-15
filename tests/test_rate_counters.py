"""E2: суточные лимиты в PostgreSQL (`rate_counters`). Применяет их E3 (IMPORT_RATE_PER_DAY)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.core.rate_counters import day_window, hit_daily
from app.db.schema import RateCounter
from app.db.session import sessionmaker

NOON = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)


async def hit(key: str, limit: int, now: datetime) -> int:
    async with sessionmaker()() as s:
        return await hit_daily(s, key, limit, now)


async def test_counts_up_to_the_limit_then_429_until_next_utc_day():
    assert [await hit("device:a", 3, NOON + timedelta(minutes=i)) for i in range(3)] == [1, 2, 3]
    with pytest.raises(ApiError) as e:
        await hit("device:a", 3, NOON)
    assert e.value.code == "rate_limited" and e.value.retry_after == 12 * 3600
    with pytest.raises(ApiError) as e:
        await hit("device:a", 3, datetime(2026, 10, 15, 23, 59, 59, 500_000, tzinfo=UTC))
    assert e.value.retry_after == 1
    # Новые сутки UTC — новое окно; другой ключ — свой счётчик.
    assert await hit("device:a", 3, datetime(2026, 10, 16, 0, 0, tzinfo=UTC)) == 1
    assert await hit("device:b", 3, NOON) == 1
    async with sessionmaker()() as s:
        rows = (await s.scalars(select(RateCounter).where(RateCounter.counter_key == "device:a"))).all()
    assert sorted((r.window_start, r.hits, r.expires_at) for r in rows) == [
        (datetime(2026, 10, 15, tzinfo=UTC), 3, datetime(2026, 10, 16, tzinfo=UTC)),
        (datetime(2026, 10, 16, tzinfo=UTC), 1, datetime(2026, 10, 17, tzinfo=UTC)),
    ]


async def test_window_is_utc_day_for_any_offset():
    msk = datetime(2026, 10, 16, 1, 30, tzinfo=UTC).astimezone()  # любое смещение → сутки UTC
    assert day_window(msk) == (datetime(2026, 10, 16, tzinfo=UTC), datetime(2026, 10, 17, tzinfo=UTC))


async def test_parallel_hits_never_exceed_the_limit():
    results = await asyncio.gather(*[hit("device:p", 7, NOON) for _ in range(15)], return_exceptions=True)
    assert sum(isinstance(r, int) for r in results) == 7
    assert sorted(r for r in results if isinstance(r, int)) == list(range(1, 8))
    assert all(r.code == "rate_limited" for r in results if isinstance(r, ApiError))
    async with sessionmaker()() as s:
        assert await s.scalar(select(RateCounter.hits).where(RateCounter.counter_key == "device:p")) == 7
