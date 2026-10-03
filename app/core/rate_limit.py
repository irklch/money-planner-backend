"""Ограничения частоты и параллельности в памяти процесса.

MVP работает одним процессом на одной VM (без Redis), поэтому этого достаточно.
При горизонтальном масштабировании лимиты нужно будет вынести в общее хранилище.
"""

import asyncio
import math
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from app.core.errors import ApiError


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: float = 60.0) -> None:
        now = time.monotonic()
        q = self._hits[key]
        while q and q[0] <= now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            retry_after = max(1, math.ceil(q[0] + window_seconds - now))
            raise ApiError("rate_limited", retry_after=retry_after)
        q.append(now)
        if len(self._hits) > 100_000:  # защита памяти
            self._hits.clear()

    def reset(self) -> None:
        self._hits.clear()


class ConcurrencyGate:
    """Ограничивает число одновременных операций: глобально и на пользователя."""

    def __init__(self) -> None:
        self._global_active = 0
        self._per_key: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, key: str, per_key_limit: int, global_limit: int):
        async with self._lock:
            if self._per_key[key] >= per_key_limit or self._global_active >= global_limit:
                raise ApiError("rate_limited", retry_after=5)
            self._per_key[key] += 1
            self._global_active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._per_key[key] -= 1
                if self._per_key[key] <= 0:
                    del self._per_key[key]
                self._global_active -= 1


limiter = SlidingWindowLimiter()
parse_gate = ConcurrencyGate()
