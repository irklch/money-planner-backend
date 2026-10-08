"""Свойства YDB, на которые опирается протокол. Результаты измерений пишутся в bench/results/."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import uuid
from pathlib import Path

import pytest
import ydb

from syncproto.engine import SyncEngine
from syncproto.models import Mutation
from syncproto.store_ydb import YdbStore

from .conftest import PERM_USERS, TEST_PREFIX, USER_A, USER_B

# Все тесты файла требуют YDB. Измерения сохраняются в bench/results/ и цитируются в REPORT.md.
pytestmark = pytest.mark.ydb
RESULTS = Path(__file__).resolve().parents[1] / "bench" / "results"
NOW = dt.datetime.now(dt.UTC)


# Мутация расхода: одинаковый i → одинаковый entityId (детерминированно), mutationId всегда новый.
def m(
    i: int | str, base: int = 0, comment: str = "c", op: str = "upsert", eid: str | None = None
) -> Mutation:
    ts = dt.datetime.now(dt.UTC)
    return Mutation(
        str(uuid.uuid4()),
        "expense",
        eid or str(uuid.uuid5(uuid.NAMESPACE_OID, str(i))),
        op,
        base,
        None,
        ts,
        ts,
        ts if op == "delete" else None,
        1,
        None
        if op == "delete"
        else {
            "amount": "10.00",
            "categoryId": str(uuid.UUID(int=1)),
            "date": "2026-10-01",
            "comment": comment,
        },
    )


# Сохранить результат измерения в bench/results/<name>.
def _save(name: str, data: object) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / name).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


# Пачка атомарна: сбой после всех UPSERT в том же запросе или падение между чтением и записью
# не оставляет ни записей, ни новой версии, ни журнала.
async def test_batch_is_atomic_when_write_fails(store: YdbStore):
    engine = SyncEngine(store, "version")
    await engine.push(USER_A, "d1", [m(1), m(2)])
    before = (await store.last_version(USER_A), await store.server_state(USER_A))

    class Failing(YdbStore):
        def __init__(self, base: YdbStore) -> None:
            super().__init__(base.pool, base.prefix)
            # Ошибка ПОСЛЕ всех UPSERT того же запроса: транзакция должна откатиться целиком.
            self._q_write += '\nSELECT Ensure(1, false, "injected failure after upserts");'

    with pytest.raises(ydb.issues.Error):
        await SyncEngine(Failing(store), "version").push(
            USER_A, "d1", [m(3), m(4), m(1, base=1, comment="x")]
        )
    after = (await store.last_version(USER_A), await store.server_state(USER_A))
    assert after == before  # ни записей, ни версии, ни журнала мутаций

    def boom(_):
        raise RuntimeError("crash between read and write")

    with pytest.raises(RuntimeError):
        await store.push(USER_A, [m(5)], boom)
    assert (await store.last_version(USER_A), await store.server_state(USER_A)) == before


# Версии атомарны при конкурентных push одного пользователя, и параллельный pull ничего не пропускает.
async def test_versions_are_atomic_under_concurrent_pushes_and_pull_never_skips(store: YdbStore):
    engine = SyncEngine(store, "version")
    n_push, per = 8, 5  # реалистичный максимум устройств ×2; предел конкуренции — в bench
    seen: dict[tuple[str, str], int] = {}
    pulls = 0
    stop = asyncio.Event()

    async def puller() -> None:
        nonlocal pulls
        cursor = 0
        while True:
            done = stop.is_set()
            page, _ = await engine.pull(USER_A, cursor, 7)
            pulls += 1
            versions = [r.version for r in page.records]
            assert versions == sorted(versions) and all(v > cursor for v in versions)
            for r in page.records:
                seen[r.key] = r.version
            cursor = page.next_cursor
            if done and not page.has_more:
                return

    async def pusher(k: int):
        return await engine.push(USER_A, f"dev{k}", [m(f"{k}-{j}") for j in range(per)])

    task = asyncio.create_task(puller())
    outs = await asyncio.gather(*(pusher(k) for k in range(n_push)))
    stop.set()
    await task
    attempts = [s.attempts for _, s in outs]
    state = await store.server_state(USER_A)
    versions = sorted(r.version for r in state.values())
    assert versions == list(range(1, n_push * per + 1))  # уникальны и без пропусков
    assert {k: r.version for k, r in state.items()} == seen  # конкурентный pull ничего не пропустил
    _save(
        "concurrency.json",
        {
            "concurrent_pushes": n_push,
            "mutations_each": per,
            "tx_attempts_total": sum(attempts),
            "tx_conflicts_retried": sum(attempts) - n_push,
            "max_attempts_one_push": max(attempts),
            "concurrent_pulls": pulls,
        },
    )


# Извлечь из JSON-плана операторы, таблицы и диапазоны чтения.
def _plan_ops(plan: object) -> tuple[set[str], set[str], list[str]]:
    s = (plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False)).replace("\\/", "/")
    names = set(re.findall(r'"Name": ?"([^"]+)"', s))
    tables = set(re.findall(r'"Table": ?"([^"]+)"', s))
    ranges = re.findall(r'"ReadRange": ?(\[[^\]]*\])', s)
    return names, tables, ranges


# Планы запросов: push читает записи точечным Lookup, pull — диапазон индекса by_version, без FullScan.
async def test_query_plans_use_keys_not_scans(store: YdbStore):
    mids = ydb.ListType(ydb.PrimitiveType.Utf8)
    kt = (
        ydb.StructType()
        .add_member("entity_type", ydb.PrimitiveType.Utf8)
        .add_member("entity_id", ydb.PrimitiveType.Utf8)
    )
    read_plan = await store.pool.explain_with_retries(
        store._q_read,
        {
            "$user_id": (USER_A, ydb.PrimitiveType.Utf8),
            "$mids": (["a"], mids),
            "$keys": ([{"entity_type": "expense", "entity_id": "x"}], ydb.ListType(kt)),
        },
    )
    pull_plan = await store.pool.explain_with_retries(
        store._q_pull,
        {
            "$user_id": (USER_A, ydb.PrimitiveType.Utf8),
            "$cursor": (5, ydb.PrimitiveType.Uint64),
            "$limit": (500, ydb.PrimitiveType.Uint64),
        },
    )
    rn, rt, _ = _plan_ops(read_plan)
    pn, pt, pr = _plan_ops(pull_plan)
    assert not any("FullScan" in n for n in rn | pn)
    assert any("Lookup" in n for n in rn), rn  # записи пачки — точечный lookup по PK
    assert {t for t in pt if "sync_records" in t} == {f"{TEST_PREFIX}/sync_records/by_version/indexImplTable"}
    assert any("user_id ($user_id)" in r and "version ($cursor, +∞)" in r for r in pr), pr
    _save(
        "query_plans.json",
        {
            "push_read": {"operators": sorted(rn), "tables": sorted(rt)},
            "pull": {"operators": sorted(pn), "tables": sorted(pt), "ranges": pr},
        },
    )


# Стоимость (RU) не растёт с размером истории пользователя — значит, сканирования нет.
async def test_cost_does_not_grow_with_user_history(store: YdbStore):
    """Фактические RU: pull страницы и push одной записи у пользователя с 3000 записей стоят
    столько же, сколько у пользователя с 30. Значит, сканирования истории нет."""
    big, small = USER_A, USER_B
    engine = SyncEngine(store, "version")
    for b in range(6):
        await engine.push(big, "seed", [m(f"big-{b}-{j}") for j in range(500)])
    await engine.push(small, "seed", [m(f"small-{j}") for j in range(30)])
    out = {}
    for name, user, cursor in (("big", big, 1500), ("small", small, 15)):
        _, pull_stats = await engine.pull(user, cursor, 10)
        _, push_stats = await engine.push(user, "d1", [m(f"{name}-new")])
        out[name] = {"pull10_ru": pull_stats.ru, "push1_ru": push_stats.ru}
    lookup_variants = {}
    for variant in ("join", "tuple_in"):
        st = YdbStore(store.pool, store.prefix, key_lookup=variant)
        # Прогрев: первое выполнение текста запроса тратит CPU на компиляцию (≈20 RU), это не чтение.
        _, s_cold = await SyncEngine(st, "version").push(small, "d1", [m(f"warm-{variant}")])
        lookup_variants[f"{variant}_first_execution_ru"] = s_cold.ru
        _, s_big = await SyncEngine(st, "version").push(big, "d1", [m(f"v-{variant}-{j}") for j in range(10)])
        _, s_small = await SyncEngine(st, "version").push(
            small, "d1", [m(f"vs-{variant}-{j}") for j in range(10)]
        )
        lookup_variants[variant] = {"push10_big_ru": s_big.ru, "push10_small_ru": s_small.ru}
    _save("cost_vs_history.json", {"by_user_size": out, "key_lookup_variants": lookup_variants})
    assert out["big"]["pull10_ru"] <= out["small"]["pull10_ru"] + 2
    assert out["big"]["push1_ru"] <= out["small"]["push1_ru"] + 2
    for variant in ("join", "tuple_in"):
        assert lookup_variants[variant]["push10_big_ru"] <= lookup_variants[variant]["push10_small_ru"] + 2


# TTL 30 дней по created_at действительно настроен на журнале мутаций.
async def test_ttl_is_configured_on_mutation_log(ydb_pool, store: YdbStore):
    driver = ydb_pool._driver
    desc = await driver.table_client.describe_table(
        f"{driver._driver_config.database}/{TEST_PREFIX}/sync_mutations"
    )
    ttl = desc.ttl_settings
    data = {
        "column": ttl.date_type_column.column_name,
        "expire_after_seconds": ttl.date_type_column.expire_after_seconds,
    }
    assert data == {"column": "created_at", "expire_after_seconds": 30 * 86400}
    _save("ttl_config.json", data)


# Фоновое удаление по TTL реально срабатывает (локально — примерно за 15 с).
@pytest.mark.slow
async def test_ttl_actually_deletes_expired_rows(ydb_pool):
    """Фоновое удаление по TTL: проверяем, успевает ли локальная YDB удалить строку за 3 минуты."""
    p = f"{TEST_PREFIX}_ttlprobe"
    await ydb_pool.execute_with_retries(f"DROP TABLE IF EXISTS `{p}`")
    await ydb_pool.execute_with_retries(
        f"CREATE TABLE `{p}` (k Uint64 NOT NULL, created_at Timestamp NOT NULL, PRIMARY KEY (k))"
        f' WITH (TTL = Interval("PT1S") ON created_at)'
    )
    await ydb_pool.execute_with_retries(
        f"UPSERT INTO `{p}` (k, created_at) VALUES (1ul, Unwrap(CurrentUtcTimestamp() - Interval('PT1H')))"
    )
    t0 = dt.datetime.now(dt.UTC)
    deleted_after = None
    for _ in range(36):
        rows = (await ydb_pool.execute_with_retries(f"SELECT COUNT(*) AS c FROM `{p}`"))[0].rows
        if rows[0]["c"] == 0:
            deleted_after = (dt.datetime.now(dt.UTC) - t0).total_seconds()
            break
        await asyncio.sleep(5)
    _save("ttl_deletion.json", {"deleted_after_seconds": deleted_after, "waited_max_seconds": 180})
    await ydb_pool.execute_with_retries(f"DROP TABLE IF EXISTS `{p}`")
    if deleted_after is None:
        pytest.xfail("local YDB did not run the TTL background job within 3 minutes")


# Пределы размера одной транзакции push (до 20 000 мутаций по ~1,1 КБ).
@pytest.mark.slow
async def test_transaction_size_limits(store: YdbStore):
    """Сколько мутаций с максимальным payload (комментарий 500 символов кириллицей) выдерживает
    одна транзакция push. Лимит API — 500; здесь проверяем запас."""
    engine = SyncEngine(store, "version")
    big_comment = "ж" * 500
    out = []
    for i, n in enumerate((500, 2000, 5000, 10000, 20000)):
        user = PERM_USERS[i]
        muts = [m(f"lim-{n}-{j}", comment=big_comment) for j in range(n)]
        approx_bytes = sum(len(json.dumps(x.payload, ensure_ascii=False).encode()) for x in muts)
        t0 = asyncio.get_running_loop().time()
        try:
            results, stats = await engine.push(user, "d1", muts)
            out.append(
                {
                    "mutations": n,
                    "payload_bytes": approx_bytes,
                    "ok": True,
                    "ru": stats.ru,
                    "seconds": round(asyncio.get_running_loop().time() - t0, 2),
                }
            )
        except Exception as e:  # фиксируем, где ломается
            out.append({"mutations": n, "payload_bytes": approx_bytes, "ok": False, "error": str(e)[:300]})
            break
    _save("tx_limits.json", out)
    assert out[0]["ok"], "API limit of 500 mutations must fit into one transaction"
