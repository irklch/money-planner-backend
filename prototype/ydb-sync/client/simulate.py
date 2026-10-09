"""Рандомизированная симуляция нескольких устройств одного пользователя.

Каждая история: 2–4 устройства с разными (в т.ч. сильно неверными) часами, офлайн-периоды,
потерянные ответы push, обрывы pull, создание/правка/удаление расходов, архивирование категорий,
очистка tombstones на сервере (устройства с отставшим курсором получают 410 и делают resync).
В конце все устройства выходят онлайн и синхронизируются до затишья. Проверяются инварианты:

- divergence    — живые записи устройства не совпали с сервером после затишья;
- stuck_outbox  — outbox не опустел;
- resurrected   — сервер подтвердил удаление, а запись в итоге жива;
- unarchived    — сервер подтвердил архивирование категории, а в итоге она не в архиве;
- causal_lost   — итоговое значение записи является предком другой правки этой же записи, т.е.
                  правка, сделанная ПОСЛЕ того, как устройство увидело итоговое значение,
                  потеряна (lost update). Происхождение отслеживается меткой в payload;
- skew_misorder — (информативно) среди конкурентных «последних» правок победила не самая поздняя
                  по реальному времени: следствие неверных часов, неизбежно для любой LWW.

Запуск: `python -m client.simulate --seeds 500` (только память) или из тестов.
"""

from __future__ import annotations

import argparse

# random — воспроизводимая случайность (seed); Counter — подсчёт нарушений по видам.
import asyncio
import datetime as dt
import random
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Тот же движок и клиент, что и в тестах, но хранилище в памяти и прямой транспорт.
from syncproto.engine import Store, SyncEngine
from syncproto.store_memory import MemoryStore

from .client import SyncClient
from .transport import DirectTransport, FaultyTransport, TransportError

# Возможные сдвиги часов устройства, секунды: верные (чаще всего), ±20 с, ±5 мин, ±2 ч, ±сутки.
SKEWS = [0, 0, 0, 20, -20, 300, -300, 7200, -7200, 86400, -86400]
# Начало виртуального времени симуляции.
T0 = dt.datetime(2026, 10, 8, 9, 0, tzinfo=dt.UTC)


# Итоги одной истории.
@dataclass
class SimResult:
    seed: int
    writes: int = 0
    pushes: int = 0
    conflicts: int = 0
    rejected: int = 0
    resyncs: int = 0
    purges: int = 0
    violations: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[Any]] = field(default_factory=dict)
    final: dict[Any, Any] = field(default_factory=dict)


# Виртуальное «настоящее» время: двигается шагами, сервер видит его без сдвига.
class _World:
    def __init__(self) -> None:
        self.t = T0

    def server_now(self) -> dt.datetime:
        return self.t


# Одна история: seed задаёт всё (устройства, сдвиги часов, действия, сбои) — её можно воспроизвести.
async def run_history(
    seed: int, store: Store | None = None, user_id: str | None = None, steps: int = 160
) -> SimResult:
    rnd = random.Random(seed)
    world = _World()
    store = store or MemoryStore()
    user_id = user_id or str(uuid.UUID(int=seed, version=4))
    engine = SyncEngine(store, clock=world.server_now)

    # Детерминированные UUID из того же генератора — одинаковые id при повторе с тем же seed.
    def ids() -> str:
        return str(uuid.UUID(int=rnd.getrandbits(128), version=4))

    # 2–4 устройства с разными часами и размерами пачек push/pull.
    n = rnd.randint(2, 4)
    devices: list[SyncClient] = []
    for i in range(n):
        skew = dt.timedelta(seconds=rnd.choice(SKEWS))
        transport = FaultyTransport(DirectTransport(engine, user_id))
        devices.append(
            SyncClient(
                ":memory:",
                f"dev{i}",
                transport,
                clock=lambda s=skew: world.t + s,
                push_batch=rnd.choice([3, 50, 500]),
                pull_limit=rnd.choice([2, 7, 500]),
                ids=ids,
            )
        )

    # Происхождение правок: каждая правка пишет в payload уникальную метку wN; parent — на какой метке
    # она основана (что устройство видело локально), true_time — реальное время, entity_of — какая запись.
    res = SimResult(seed)
    parent: dict[str, str | None] = {}  # метка правки → метка правки, на которой она основана
    true_time: dict[str, dt.datetime] = {}
    entity_of: dict[str, tuple[str, str]] = {}
    tag_n = 0

    def tag() -> str:
        nonlocal tag_n
        tag_n += 1
        return f"w{tag_n}"

    # Метка, которую устройство сейчас видит у записи (из comment расхода или name категории).
    def local_tag(c: SyncClient, et: str, eid: str) -> str | None:
        p = c.visible(et).get(eid)
        if p is None:
            return None
        return p.get("comment") if et == "expense" else p.get("name")

    cat_ids: list[str] = []
    # Основной цикл: на каждом шаге случайное устройство делает случайное действие.
    for _ in range(steps):
        world.t += dt.timedelta(seconds=rnd.choice([0, 0.001, 1, 5, 30, 120]))
        c = rnd.choice(devices)
        tr: FaultyTransport = c.transport  # type: ignore[assignment]
        roll = rnd.random()
        exp = list(c.visible("expense"))
        # Категории, доступные устройству для нового расхода (архивные — нельзя).
        active = list(c.active_categories())
        # ~12 % — новая категория, ~23 % — новый расход, ~27 % — правка расхода, ~6 % — правка категории,
        # ~6 % — удаление, ~6 % — переключение офлайн, ~2 % — архивирование категории,
        # ~2 % — очистка tombstones на сервере, остальное — sync со случайными сбоями сети.
        if roll < 0.12 or not cat_ids or not active:
            w = tag()
            cat_ids.append(c.create_category(w))
            parent[w], true_time[w], entity_of[w] = None, world.t, ("category", cat_ids[-1])
        elif roll < 0.35:
            w = tag()
            eid = c.create_expense(f"{rnd.randint(1, 9999)}.00", rnd.choice(active), "2026-10-01", w)
            parent[w], true_time[w], entity_of[w] = None, world.t, ("expense", eid)
        elif roll < 0.62 and exp:
            eid = rnd.choice(exp)
            w = tag()
            parent[w], true_time[w], entity_of[w] = local_tag(c, "expense", eid), world.t, ("expense", eid)
            c.update("expense", eid, comment=w, amount=f"{rnd.randint(1, 9999)}.00")
        elif roll < 0.68:
            cats = list(c.visible("category"))
            if cats:
                cid = rnd.choice(cats)
                w = tag()
                parent[w], true_time[w], entity_of[w] = (
                    local_tag(c, "category", cid),
                    world.t,
                    ("category", cid),
                )
                c.update("category", cid, name=w)
        elif roll < 0.74 and exp:
            c.delete("expense", rnd.choice(exp))
        elif roll < 0.80:
            tr.offline = not tr.offline
        elif roll < 0.82 and len(active) > 1:
            c.archive_category(rnd.choice(active))
        elif roll < 0.84:
            # Очистить все tombstones: устройства с курсором меньше последней версии получат 410.
            await store.purge_tombstones(user_id, await store.last_version(user_id))  # type: ignore[attr-defined]
            res.purges += 1
        else:
            if rnd.random() < 0.1:
                tr.drop_push_responses = 1
            if rnd.random() < 0.1:
                tr.fail_pull_after(rnd.randint(0, 2))
            r = await c.sync()
            res.conflicts += r.conflicts
            res.rejected += r.rejected
            res.pushes += r.push_requests
            res.resyncs += r.resyncs
    res.writes = tag_n

    # Затишье: все онлайн, без сбоев, несколько раундов.
    for c in devices:
        tr = c.transport  # type: ignore[assignment]
        tr.offline, tr.drop_push_responses, tr.fail_pull_after_pages = False, 0, None
    for _ in range(3):
        for c in devices:
            r = await c.sync()
            res.resyncs += r.resyncs
            if not r.ok:
                raise TransportError("sync failed in quiescence")

    # Итоговое состояние сервера и проверки инвариантов.
    pull = await _server_records(engine, user_id)
    server = {k: (v["payload"], v["deletedAt"] is not None) for k, v in pull.items()}
    res.final = server
    server_live = {k: p for k, (p, deleted) in server.items() if not deleted}
    for c in devices:
        if c.outbox_size():
            res.violations["stuck_outbox"] += 1
        # Сравниваем живые записи: tombstone, очищенный на сервере, может остаться у устройства.
        if c.live() != server_live:
            res.violations["divergence"] += 1

    # Подтверждённые удаления: в истории сервера есть tombstone ⇒ запись не должна быть живой
    # (после очистки tombstones записи может не быть вовсе — это тоже «удалена»).
    acked_deleted = getattr(store, "ever_deleted", None)
    if acked_deleted is not None:
        for key in acked_deleted(user_id):
            if key in server_live:
                res.violations["resurrected"] += 1
    # Подтверждённое архивирование категории окончательно (archive wins).
    acked_archived = getattr(store, "ever_archived", None)
    if acked_archived is not None:
        for key in acked_archived(user_id):
            if not server_live.get(key, {}).get("isArchived"):
                res.violations["unarchived"] += 1

    # Предки каждой правки (транзитивно) — для поиска потерянных причинно-поздних правок.
    ancestors: dict[str, set[str]] = {}

    def anc(w: str) -> set[str]:
        if w not in ancestors:
            p = parent.get(w)
            ancestors[w] = set() if p is None else {p} | anc(p)
        return ancestors[w]

    # Правки, сгруппированные по записям.
    by_entity: dict[tuple[str, str], list[str]] = {}
    for w, key in entity_of.items():
        by_entity.setdefault(key, []).append(w)
    # Для каждой живой записи на сервере: итоговая метка не должна быть предком другой правки этой записи.
    for key, (payload, deleted) in server.items():
        # Удалённые расходы и архивные категории: потеря конкурентной правки — правило delete/archive wins.
        if deleted or payload is None or payload.get("isArchived"):
            continue
        final = payload.get("comment") if key[0] == "expense" else payload.get("name")
        writes = by_entity.get(key, [])
        if any(final in anc(w) for w in writes):
            res.violations["causal_lost"] += 1
            res.examples.setdefault("causal_lost", []).append(key)
            continue
        # Конкурентные «листья»: правки записи, не являющиеся предками других её правок.
        leaves = [w for w in writes if not any(w in anc(x) for x in writes)]
        if len(leaves) > 1 and max(leaves, key=lambda w: true_time[w]) != final:
            res.violations["skew_misorder"] += 1
    # Закрыть in-memory SQLite устройств.
    for c in devices:
        c.close()
    return res


# Прочитать всё серверное состояние пользователя через pull страницами (как новый телефон).
async def _server_records(engine: SyncEngine, user_id: str) -> dict[Any, dict[str, Any]]:
    out: dict[Any, dict[str, Any]] = {}
    cursor = 0
    while True:
        page, _ = await engine.pull(user_id, cursor, 500)
        for r in page.records:
            out[(r.entity_type, r.entity_id)] = r.to_api().model_dump(mode="json", by_alias=True)
        cursor = page.next_cursor
        if not page.has_more:
            return out


class TrackingMemoryStore(MemoryStore):
    """MemoryStore, запоминающий ключи, по которым сервер когда-либо применил удаление или архивирование."""

    def __init__(self) -> None:
        super().__init__()
        self._deleted: dict[str, set[Any]] = {}
        self._archived: dict[str, set[Any]] = {}

    async def push(self, user_id, mutations, planner):  # type: ignore[override]
        plan, stats = await super().push(user_id, mutations, planner)
        for k, r in plan.upserts.items():
            if r.deleted:
                self._deleted.setdefault(user_id, set()).add(k)
            elif r.payload and r.payload.get("isArchived"):
                self._archived.setdefault(user_id, set()).add(k)
        return plan, stats

    def ever_deleted(self, user_id: str) -> set[Any]:
        return self._deleted.get(user_id, set())

    def ever_archived(self, user_id: str) -> set[Any]:
        return self._archived.get(user_id, set())


# Прогнать seed-ы и свести нарушения и статистику в таблицу.
async def run_many(seeds: range, steps: int = 160) -> dict[str, Any]:
    agg: Counter[str] = Counter()
    histories_with: Counter[str] = Counter()
    writes = conflicts = resyncs = purges = 0
    for seed in seeds:
        r = await run_history(seed, TrackingMemoryStore(), steps=steps)
        writes += r.writes
        conflicts += r.conflicts
        resyncs += r.resyncs
        purges += r.purges
        agg.update(r.violations)
        histories_with.update({k: 1 for k, v in r.violations.items() if v})
    return {
        "histories": len(seeds),
        "writes": writes,
        "conflicts": conflicts,
        "purges": purges,
        "resyncs": resyncs,
        "violations": dict(agg),
        "histories_with": dict(histories_with),
    }


# CLI: python -m client.simulate --seeds 1000 [--steps N].
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", type=int, default=300)
    p.add_argument("--steps", type=int, default=160)
    a = p.parse_args()
    import json

    out = asyncio.run(run_many(range(a.seeds), a.steps))
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
