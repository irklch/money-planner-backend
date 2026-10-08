"""Алгоритм push/pull, не зависящий от хранилища.

Push = прочитать снимок (состояние пользователя, журнал мутаций, текущие записи) → `plan_push`
(чистая функция) → записать план. Хранилище обязано выполнить чтение и запись в одной
serializable-транзакции и при конфликте транзакции повторить ВСЁ, включая планирование.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import Mutation, MutationResult, PushPlan, StoredRecord
from .resolve import STRATEGIES, Strategy


@dataclass(slots=True)
class PushSnapshot:
    last_version: int
    tombstone_horizon: int
    logs: dict[str, tuple[str, dict[str, Any]]]  # mutation_id -> (request_hash, stored result)
    records: dict[tuple[str, str], StoredRecord]


@dataclass(slots=True)
class OpStats:
    """Измерения одной операции хранилища (заполняет YDB-хранилище)."""

    ru: int = 0
    ydb_calls: int = 0
    attempts: int = 1  # >1 — были конфликты транзакций (ABORTED) и повторы
    calls_by_method: dict[str, int] = field(default_factory=dict)
    # Фактическая статистика YDB (stats_mode=BASIC), суммарно по запросам последней попытки.
    read_rows: int = 0
    read_bytes: int = 0
    write_rows: int = 0
    write_bytes: int = 0
    cpu_us: int = 0
    tables: dict[str, dict[str, int]] = field(default_factory=dict)
    # RU по официальной формуле из этих фактических строк/байт — ПРОГНОЗ, не измерение.
    ru_io_formula: int = 0


@dataclass(slots=True)
class PullPage:
    records: list[StoredRecord]
    next_cursor: int
    has_more: bool


class ResyncRequired(Exception):
    """Курсор старше горизонта очистки tombstones: клиенту нужна полная перезагрузка."""


class Store(Protocol):
    async def push(
        self, user_id: str, mutations: list[Mutation], planner: Callable[[PushSnapshot], PushPlan]
    ) -> tuple[PushPlan, OpStats]: ...

    async def pull(self, user_id: str, cursor: int, limit: int) -> tuple[PullPage, OpStats]: ...


def plan_push(
    snap: PushSnapshot, mutations: list[Mutation], device_id: str, strategy: Strategy, now: dt.datetime
) -> PushPlan:
    version = snap.last_version
    current = dict(snap.records)
    plan = PushPlan(results=[], last_version=version)
    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    # Перебазирование внутри одного запроса: клиент не знает версию, которую сервер присвоит
    # его первой правке, поэтому вторая правка той же записи в той же пачке несёт старую базу.
    chain: dict[tuple[str, str], tuple[int, int]] = {}  # key -> (база, полученная версия)

    for m in mutations:
        h = m.request_hash(device_id)
        prior = seen.get(m.mutation_id) or snap.logs.get(m.mutation_id)
        cur = current.get(m.key)
        if prior is not None:
            prior_hash, stored = prior
            if prior_hash != h:
                plan.results.append(
                    MutationResult(m.mutation_id, "rejected", reason="mutation_id_reused", record=cur)
                )
                continue
            r = MutationResult.from_stored(m.mutation_id, stored)
            # Повтор: возвращаем сохранённый результат и прикладываем актуальную запись, если она
            # изменилась с тех пор или мутация не была применена: иначе клиент «застрянет» на
            # своей версии (найдено симуляцией: повтор отклонённой правки после потери ответа).
            if cur is not None and (r.status != "applied" or cur.version != r.version):
                r.record = cur
            if r.status == "applied" and r.version is not None and r.base_version is not None:
                chain[m.key] = (r.base_version, r.version)
            plan.results.append(r)
            continue

        base = m.base_version
        link = chain.get(m.key)
        if link is not None and base == link[0]:
            base = link[1]

        d = strategy.decide(cur, m, device_id, now, base)
        if d.apply:
            version += 1
            rec = StoredRecord(
                entity_type=m.entity_type,
                entity_id=m.entity_id,
                version=version,
                created_at=cur.created_at if cur else m.created_at,
                updated_at=m.updated_at,
                deleted_at=m.deleted_at if m.op == "delete" else None,
                hlc=d.hlc,
                order_ts=d.order_ts or m.updated_at,
                device_id=device_id,
                schema_version=m.schema_version,
                payload=None if m.op == "delete" else m.payload,
                mutation_id=m.mutation_id,
            )
            current[m.key] = rec
            plan.upserts[m.key] = rec
            # Исходная база сохраняется на всю цепочку: create → edit → edit в одной пачке.
            chain[m.key] = (link[0] if link is not None and base == link[1] else base, version)
            r = MutationResult(
                m.mutation_id, "applied", conflict=d.conflict, version=version, base_version=base
            )
        else:
            r = MutationResult(
                m.mutation_id,
                d.status,
                reason=d.reason,
                conflict=d.conflict,
                version=cur.version if cur else None,
                record=cur,
            )
        stored = r.stored_json()
        plan.new_logs.append((m.mutation_id, h, stored))
        seen[m.mutation_id] = (h, stored)
        plan.results.append(r)

    plan.last_version = version
    plan.changed = bool(plan.upserts or plan.new_logs)
    return plan


class InvalidMutation(ValueError):
    pass


class SyncEngine:
    def __init__(
        self,
        store: Store,
        strategy: str | Strategy = "version",
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.store = store
        self.strategy = STRATEGIES[strategy] if isinstance(strategy, str) else strategy
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def push(
        self, user_id: str, device_id: str, mutations: list[Mutation]
    ) -> tuple[list[MutationResult], OpStats]:
        if self.strategy.needs_hlc and any(m.hlc is None for m in mutations):
            raise InvalidMutation(f"strategy {self.strategy.name} requires hlc on every mutation")

        def planner(snap: PushSnapshot) -> PushPlan:
            return plan_push(snap, mutations, device_id, self.strategy, self._clock())

        plan, stats = await self.store.push(user_id, mutations, planner)
        return plan.results, stats

    async def pull(self, user_id: str, cursor: int, limit: int) -> tuple[PullPage, OpStats]:
        return await self.store.pull(user_id, cursor, limit)
