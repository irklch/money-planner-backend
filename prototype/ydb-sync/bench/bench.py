"""Воспроизводимый benchmark sync API поверх YDB.

    python -m bench.bench --target local            # in-process FastAPI + локальная YDB
    python -m bench.bench --target url --url https://<gateway>   # облако (только после подтверждения)

Что измеряется (на каждую операцию, N повторов):
- latency end-to-end (клиент → API → YDB → клиент), p50/p95/max;
- обращения к YDB (gRPC ExecuteQuery/Commit/...), попытки транзакций (конфликты = попытки − 1);
- `ru_header` — RU из `x-ydb-consumed-units`, как их вернул сервер YDB;
- фактические строки/байты чтения и записи по таблицам и CPU (статистика YDB, stats_mode=BASIC);
- `ru_formula` — RU по официальной формуле из ЭТИХ фактических строк/байт (только ввод-вывод).
  Это прогноз: локальная YDB не тарифицирует записи (см. отчёт), в облаке сверяем с ru_header;
- размеры тела запроса и ответа.

Данные синтетические, пользователи — фиксированные тестовые UUID.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import platform
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from syncproto import auth

# Куда сохраняются результаты; фиксированные синтетические пользователи по ролям benchmark.
RESULTS = Path(__file__).resolve().parent / "results"
BENCH_USERS = {
    name: f"00000000-0000-4000-8000-0000000b{i:04d}"
    for i, name in enumerate(["writer", "reader", "initial", "replay", "contention", "small", "variant"])
}
# Категория для синтетических расходов (её существование сервер не проверяет).
CATEGORY_ID = "00000000-0000-4000-8000-00000000ca7e"


# Минимальный HTTP-клиент benchmark: отправляет запрос и возвращает (JSON, измерения).
class Client:
    def __init__(self, http: httpx.AsyncClient, token: str, device: str) -> None:
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.device = device

    async def push(self, mutations: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        body = json.dumps(
            {"deviceId": self.device, "mutations": mutations}, separators=(",", ":"), ensure_ascii=False
        ).encode()
        t0 = time.perf_counter()
        r = await self.http.post("/sync/push", content=body, headers=self.headers)
        ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        return r.json(), _measure(r, ms, len(body))

    async def pull(self, cursor: int, limit: int) -> tuple[dict[str, Any], dict[str, Any]]:
        t0 = time.perf_counter()
        r = await self.http.get("/sync/pull", params={"cursor": cursor, "limit": limit}, headers=self.headers)
        ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        return r.json(), _measure(r, ms, 0)


# Измерения одного запроса: latency, время приложения, RU, обращения к YDB, попытки, строки/байты, размеры.
def _measure(r: httpx.Response, ms: float, req_bytes: int) -> dict[str, Any]:
    st = json.loads(r.headers.get("x-ydb-stats", "{}"))
    return {
        "latency_ms": ms,
        "app_ms": float(r.headers.get("server-timing", "app;dur=0").split("dur=")[1]),
        "ru_header": int(r.headers.get("x-ydb-ru", 0)),
        "ru_formula": st.get("ruIoFormula"),
        "ydb_calls": int(r.headers.get("x-ydb-calls", 0)),
        "attempts": int(r.headers.get("x-ydb-attempts", 1)),
        "read_rows": st.get("readRows"),
        "write_rows": st.get("writeRows"),
        "write_bytes": st.get("writeBytes"),
        "cpu_us": st.get("cpuUs"),
        "tables": st.get("tables"),
        "request_bytes": req_bytes,
        "response_bytes": len(r.content),
    }


# Синтетическая мутация расхода типичного размера.
def expense(i: int, base: int = 0, entity_id: str | None = None, op: str = "upsert") -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).isoformat()
    return {
        "mutationId": str(uuid.uuid4()),
        "entityType": "expense",
        "entityId": entity_id or str(uuid.uuid4()),
        "op": op,
        "baseVersion": base,
        "hlc": f"{int(time.time() * 1000):015d}.{i % 1000000:06d}",
        "createdAt": now,
        "updatedAt": now,
        "deletedAt": now if op == "delete" else None,
        # Синтетический расход типичного размера: сумма, категория, дата, комментарий ~40 символов.
        "payload": None
        if op == "delete"
        else {
            "amount": f"{(i * 37) % 9000 + 100}.{i % 100:02d}",
            "categoryId": CATEGORY_ID,
            "date": "2026-10-01",
            "comment": f"Синтетическая покупка №{i} в магазине",
        },
    }


# Свести серию замеров: перцентили latency и медианы остальных метрик.
def summarize(name: str, samples: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
    lat = sorted(s["latency_ms"] for s in samples)

    def pct(p: float) -> float:
        return round(lat[min(len(lat) - 1, int(round(p * (len(lat) - 1))))], 1)

    def med(key: str) -> Any:
        vals = [s[key] for s in samples if s.get(key) is not None]
        return statistics.median(vals) if vals else None

    return {
        "op": name,
        "n": len(samples),
        "note": note,
        "latency_p50_ms": pct(0.5),
        "latency_p95_ms": pct(0.95),
        "latency_max_ms": round(lat[-1], 1),
        "app_p50_ms": med("app_ms"),
        "ydb_calls": med("ydb_calls"),
        "tx_conflicts": sum(s["attempts"] - 1 for s in samples),
        "ru_header": med("ru_header"),
        "ru_formula": med("ru_formula"),
        "read_rows": med("read_rows"),
        "write_rows": med("write_rows"),
        "write_bytes": med("write_bytes"),
        "cpu_us": med("cpu_us"),
        "request_bytes": med("request_bytes"),
        "response_bytes": med("response_bytes"),
        "tables": samples[-1].get("tables"),
    }


# Весь набор операций из ТЗ на одном экземпляре API.
async def run(http: httpx.AsyncClient, token_for, n: int, wipe=None) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    if wipe:
        await wipe(list(BENCH_USERS.values()))

    # Клиент от имени одного из пользователей benchmark.
    def cl(user: str, device: str = "bench-device") -> Client:
        return Client(http, token_for(BENCH_USERS[user]), device)

    w = cl("writer")
    # Прогрев: компиляция запросов и пул сессий. В результаты не входит.
    for _ in range(3):
        await w.push([expense(0)])
        await w.pull(0, 1)

    # Повторить операцию `times` раз и сохранить сводку.
    async def repeat(name: str, fn, times: int = n, note: str = "") -> None:
        samples = []
        for i in range(times):
            samples.append(await fn(i))
        out.append(summarize(name, samples, note))
        print(
            f"  {name:28s} p50={out[-1]['latency_p50_ms']:7.1f}ms ru_formula={out[-1]['ru_formula']}"
            f" ru_header={out[-1]['ru_header']} calls={out[-1]['ydb_calls']}",
            flush=True,
        )

    # Созданные записи (id и версия) — чтобы затем обновлять и удалять именно их.
    created: list[tuple[str, int]] = []

    async def create1(i: int):
        m = expense(i)
        resp, meas = await w.push([m])
        created.append((m["entityId"], resp["results"][0]["version"]))
        return meas

    # Создание, обновление (с правильной baseVersion) и удаление одной записи.
    await repeat("create_1", create1)

    async def update1(i: int):
        eid, ver = created[i]
        resp, meas = await w.push([expense(i + 1000, base=ver, entity_id=eid)])
        created[i] = (eid, resp["results"][0]["version"])
        return meas

    await repeat("update_1", update1)

    async def delete1(i: int):
        eid, ver = created[i]
        _, meas = await w.push([expense(i, base=ver, entity_id=eid, op="delete")])
        return meas

    await repeat("delete_1", delete1)
    # Пачки push разного размера.
    for size in (8, 10, 100):
        await repeat(f"push_{size}", lambda i, s=size: _push_new(w, s, i * s))
    await repeat("push_500", lambda i: _push_new(w, 500, i * 500), times=max(3, n // 5))

    # Читатель с 3000 записями: pull с курсора, гарантирующего ровно N записей.
    # Наполняем пользователя 3000 записями (6 × 500), версии будут 1..3000.
    r = cl("reader")
    for b in range(6):
        await _push_new(r, 500, b * 500)
    last = 3000  # свежий пользователь: версии 1..3000
    await repeat("pull_0_no_changes", lambda i: _pull(r, last, 500), note="самый частый запрос")
    for size in (10, 100, 500):
        await repeat(f"pull_{size}", lambda i, s=size: _pull(r, last - s, s))

    # Initial sync 1000: выгрузка с устройства (2 × push 500) и загрузка на новое (pull по 500).
    async def initial_upload(i: int):
        c = Client(http, token_for(str(uuid.UUID(int=0xB0000 + i, version=4))), "fresh")
        a, b = await _push_new(c, 500, 0), await _push_new(c, 500, 500)
        return _combine([a, b])

    await repeat("initial_upload_1000", initial_upload, times=max(3, n // 5), note="2 запроса × 500")
    # Пользователь с 1000 записями для замера восстановления на новом телефоне.
    ini = cl("initial")
    for b in range(2):
        await _push_new(ini, 500, b * 500)

    async def initial_download(i: int):
        cursor, parts = 0, []
        while True:
            page, meas = await ini.pull(cursor, 500)
            parts.append(meas)
            cursor = page["nextCursor"]
            if not page["hasMore"]:
                return _combine(parts)

    await repeat("initial_download_1000", initial_download, times=max(3, n // 5), note="страницы по 500")

    # Повторная отправка уже обработанной пачки (100 мутаций).
    rp = cl("replay")
    batch = [expense(i) for i in range(100)]
    await rp.push(batch)
    await repeat("repush_processed_100", lambda i: _replay(rp, batch))
    return {"operations": out}


# Push `size` новых записей.
async def _push_new(c: Client, size: int, offset: int) -> dict[str, Any]:
    _, meas = await c.push([expense(offset + j) for j in range(size)])
    return meas


# Pull одной страницы; число записей — в измерения.
async def _pull(c: Client, cursor: int, limit: int) -> dict[str, Any]:
    page, meas = await c.pull(cursor, limit)
    meas["records"] = len(page["records"])
    return meas


# Повтор уже обработанной пачки: все результаты должны быть replayed.
async def _replay(c: Client, batch: list[dict[str, Any]]) -> dict[str, Any]:
    resp, meas = await c.push(batch)
    assert all(r["replayed"] for r in resp["results"])
    return meas


# Сложить измерения нескольких запросов в одно (initial sync из нескольких запросов).
def _combine(parts: list[dict[str, Any]]) -> dict[str, Any]:
    out = {
        k: sum(p[k] or 0 for p in parts)
        for k in (
            "latency_ms",
            "app_ms",
            "ru_header",
            "ydb_calls",
            "read_rows",
            "write_rows",
            "write_bytes",
            "cpu_us",
            "request_bytes",
            "response_bytes",
        )
    }
    out["ru_formula"] = sum(p["ru_formula"] or 0 for p in parts)
    out["attempts"] = 1 + sum(p["attempts"] - 1 for p in parts)
    out["requests"] = len(parts)
    return out


# Конкуренция: K устройств одного пользователя одновременно отправляют push по 8 мутаций.
async def contention(http: httpx.AsyncClient, token_for, levels=(1, 2, 4, 8, 16, 24)) -> list[dict[str, Any]]:
    """K устройств одного пользователя одновременно отправляют push по 8 мутаций."""
    res = []
    for k in levels:
        user = str(uuid.UUID(int=0xC0000 + k, version=4))
        clients = [Client(http, token_for(user), f"dev{j}") for j in range(k)]

        async def one(c: Client) -> dict[str, Any] | str:
            try:
                return await _push_new(c, 8, 0)
            except httpx.HTTPStatusError as e:
                return f"http {e.response.status_code}"

        t0 = time.perf_counter()
        outs = await asyncio.gather(*(one(c) for c in clients))
        wall = (time.perf_counter() - t0) * 1000
        ok = [o for o in outs if isinstance(o, dict)]
        res.append(
            {
                "concurrent_devices": k,
                "ok": len(ok),
                "failed_503": sum(1 for o in outs if o == "http 503"),
                "tx_conflicts": sum(o["attempts"] - 1 for o in ok),
                "max_attempts": max(o["attempts"] for o in ok),
                "latency_max_ms": round(max(o["latency_ms"] for o in ok), 1),
                "wall_ms": round(wall, 1),
            }
        )
        print("  contention", res[-1], flush=True)
    return res


# Сравнение вариантов схемы на одинаковых операциях (журнал мутаций, COVER, способ чтения ключей).
async def variants(pool, prefix: str, token_for) -> dict[str, Any]:
    """Сравнение вариантов схемы на одинаковых операциях (фактические строки/байты)."""
    from syncproto import schema
    from syncproto.api import create_app
    from syncproto.store_ydb import YdbStore

    from .local import local_settings

    out: dict[str, Any] = {}
    nocover = f"{prefix}_nocover"
    await schema.apply(pool, schema.drop_ddl(nocover) + schema.ddl(nocover, cover=False))
    cases = {
        "baseline(cover+log+join)": YdbStore(pool, prefix, collect_stats=True),
        "no_mutation_log": YdbStore(pool, prefix, collect_stats=True, mutation_log=False),
        "index_without_cover": YdbStore(pool, nocover, collect_stats=True),
        "lookup_tuple_in": YdbStore(pool, prefix, collect_stats=True, key_lookup="tuple_in"),
    }
    for name, store in cases.items():
        app = create_app(local_settings(prefix, "version", collect=True), store=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as http:
            user = str(uuid.uuid5(uuid.NAMESPACE_OID, f"variant-{name}"))
            c = Client(http, token_for(user), "dev")
            for b in range(4):  # история 2000 записей
                await _push_new(c, 500, b * 500)
            push10 = [await _push_new(c, 10, 5000 + i * 10) for i in range(5)]
            pull100 = [await _pull(c, 1000, 100) for _ in range(5)]
            out[name] = {"push_10": summarize("push_10", push10), "pull_100": summarize("pull_100", pull100)}
            print(
                f"  variant {name}: push10 ru_formula={out[name]['push_10']['ru_formula']}"
                f" pull100 ru_formula={out[name]['pull_100']['ru_formula']}",
                flush=True,
            )
    await asyncio.sleep(20)  # partition_stats обновляются периодически
    out["storage_index_without_cover"] = await storage_per_record(pool, nocover)
    await schema.apply(pool, schema.drop_ddl(nocover))
    return out


# Фактический размер таблиц из системного представления YDB .sys/partition_stats.
async def storage_per_record(pool, prefix: str) -> dict[str, Any]:
    """Фактический размер данных из .sys/partition_stats (обновляется с задержкой)."""
    q = f"""SELECT Path, SUM(DataSize) AS bytes, SUM(RowCount) AS rows FROM `.sys/partition_stats`
            WHERE Path LIKE '%/{prefix}/%' GROUP BY Path"""
    rows = (await pool.execute_with_retries(q))[0].rows
    return {
        r["Path"].split(f"/{prefix}/")[-1]: {"bytes": int(r["bytes"]), "rows": int(r["rows"])} for r in rows
    }


# Локальный прогон: своя схема с префиксом, API в памяти, сохранение JSON с результатами.
async def main_local(n: int, out_name: str) -> None:
    import ydb

    from syncproto import schema
    from syncproto.api import create_app
    from syncproto.store_ydb import YdbStore, open_driver

    from .local import local_settings

    prefix = os.environ.get("YDB_BENCH_PREFIX", "ydbsync_bench")
    s = local_settings(prefix, "version", collect=True)
    driver = await open_driver(s)
    pool = ydb.aio.QuerySessionPool(driver, size=40)
    await schema.apply(pool, schema.drop_ddl(prefix) + schema.ddl(prefix))
    store = YdbStore(pool, prefix, collect_stats=True)
    app = create_app(s, store=store)

    def token_for(u: str) -> str:
        return auth.issue_test_token(s.jwt_secret, u, s.jwt_audience)

    result: dict[str, Any] = {
        "target": "local",
        "when": dt.datetime.now(dt.UTC).isoformat(),
        "environment": {
            "machine": platform.machine(),
            "python": platform.python_version(),
            "ydb": "ydbplatform/local-ydb:latest (amd64 под Rosetta в colima, in-memory PDisk)",
            "api": "FastAPI in-process (httpx.ASGITransport), без сети между клиентом и API",
        },
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as http:
        print("operations:", flush=True)
        result.update(await run(http, token_for, n, wipe=store.wipe_users))
        print("contention:", flush=True)
        result["contention"] = await contention(http, token_for)
    print("variants:", flush=True)
    result["variants"] = await variants(pool, prefix, token_for)
    await asyncio.sleep(20)  # partition_stats обновляются периодически
    result["storage"] = await storage_per_record(pool, prefix)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / out_name).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"saved {RESULTS / out_name}")
    await pool.stop()
    await driver.stop()


# Облачный прогон через API Gateway (только после подтверждения облачного этапа).
async def main_url(url: str, n: int, out_name: str) -> None:
    """Облачный прогон: токены выпускаются локально секретом из Lockbox (SYNC_JWT_SECRET в env)."""
    secret = os.environ["SYNC_JWT_SECRET"]

    def token_for(u: str) -> str:
        return auth.issue_test_token(secret, u, "money-planner-sync-proto")

    result: dict[str, Any] = {"target": url, "when": dt.datetime.now(dt.UTC).isoformat()}
    async with httpx.AsyncClient(base_url=url, timeout=60) as http:
        result.update(await run(http, token_for, n))
        result["contention"] = await contention(http, token_for, levels=(1, 2, 4, 8))
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / out_name).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")


# CLI: --target local | url, -n повторов, --out имя файла результата.
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--target", choices=["local", "url"], default="local")
    p.add_argument("--url")
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--out")
    a = p.parse_args()
    if a.target == "local":
        asyncio.run(main_local(a.n, a.out or "local_benchmark.json"))
    else:
        asyncio.run(main_url(a.url, a.n, a.out or "cloud_benchmark.json"))


if __name__ == "__main__":
    main()
