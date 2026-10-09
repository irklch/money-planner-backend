"""Хранилище в памяти с той же семантикой, что и YDB (сериализация push по пользователю).

Используется в рандомизированной симуляции: тысячи историй за секунды. Эквивалентность с YDB
проверяется отдельным тестом на тех же seed-ах.
"""

from __future__ import annotations

# asyncio.Lock — сериализация push одного пользователя (как OCC-транзакция в YDB).
import asyncio
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .engine import OpStats, PullPage, PushSnapshot, check_horizon
from .models import Mutation, PushPlan, StoredRecord


# Данные одного пользователя: счётчик версий, горизонт tombstones, записи, журнал мутаций.
@dataclass
class _User:
    last_version: int = 0
    tombstone_horizon: int = 0
    records: dict[tuple[str, str], StoredRecord] = field(default_factory=dict)
    logs: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Все пользователи; новый создаётся при первом обращении.
class MemoryStore:
    def __init__(self) -> None:
        self.users: dict[str, _User] = defaultdict(_User)

    # Push под замком пользователя: снимок → план → применение плана.
    async def push(
        self, user_id: str, mutations: list[Mutation], planner: Callable[[PushSnapshot], PushPlan]
    ) -> tuple[PushPlan, OpStats]:
        u = self.users[user_id]
        async with u.lock:
            # Снимок содержит только то же, что читает YDB-хранилище: записи и журнал по ключам этой пачки.
            keys = {m.key for m in mutations}
            snap = PushSnapshot(
                last_version=u.last_version,
                tombstone_horizon=u.tombstone_horizon,
                logs={m.mutation_id: u.logs[m.mutation_id] for m in mutations if m.mutation_id in u.logs},
                records={k: u.records[k] for k in keys if k in u.records},
            )
            plan = planner(snap)
            u.records.update(plan.upserts)
            for mid, h, res in plan.new_logs:
                u.logs[mid] = (h, res)
            u.last_version = plan.last_version
        return plan, OpStats()

    # Pull: записи с версией больше курсора, по возрастанию версии, не больше limit.
    async def pull(
        self, user_id: str, cursor: int, limit: int, known_horizon: int = 0
    ) -> tuple[PullPage, OpStats]:
        u = self.users[user_id]
        # Клиент мог пропустить очищенные tombstones — нужна полная перезагрузка (410).
        check_horizon(cursor, known_horizon, u.tombstone_horizon)
        rows = sorted((r for r in u.records.values() if r.version > cursor), key=lambda r: r.version)
        page = rows[:limit]
        next_cursor = page[-1].version if page else cursor
        return PullPage(page, next_cursor, len(rows) > limit, u.tombstone_horizon), OpStats()

    # Очистка tombstones с версией <= horizon и сдвиг горизонта (как YdbStore.purge_tombstones).
    async def purge_tombstones(self, user_id: str, horizon: int) -> None:
        u = self.users[user_id]
        async with u.lock:
            u.records = {k: r for k, r in u.records.items() if not (r.deleted and r.version <= horizon)}
            u.tombstone_horizon = max(u.tombstone_horizon, horizon)  # горизонт не убывает

    # Последняя выданная версия пользователя.
    async def last_version(self, user_id: str) -> int:
        return self.users[user_id].last_version

    # Всё серверное состояние пользователя — для проверок в тестах.
    def server_state(self, user_id: str) -> dict[tuple[str, str], StoredRecord]:
        return dict(self.users[user_id].records)
