"""Облачная проверка sync end-to-end через API Gateway (запускать только после cloud/deploy.sh).

    export GATEWAY_URL=https://<domain>  SYNC_JWT_SECRET=$(yc lockbox payload get ... --key jwt)
    python -m cloud.e2e [--restore 200] [--bulk 1000]

Тот же клиент (SQLite + outbox), что в локальных тестах, но по HTTP через шлюз в Serverless
Container и YDB Serverless. Каждый HTTP-запрос записывается: шаг, путь, статус, latency, RU из
`X-YDB-RU` (облачная YDB тарифицирует и чтение, и запись), обращения к YDB, время приложения.

Шаги: защита API → создание (push) → получение (pull) → правка → удаление → конфликт двух
устройств → повтор после потерянного ответа → архивирование категории → восстановление истории
на новом устройстве → (опционально) первичная выгрузка `--bulk` записей при текущем лимите RU/с.

Все данные синтетические, пользователи — фиксированные тестовые UUID. Результат:
bench/results/cloud_e2e.json (без адреса шлюза и токенов).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from client.client import SyncClient
from client.transport import FaultyTransport, HttpTransport, TransportError
from syncproto import auth

RESULTS = Path(__file__).resolve().parents[1] / "bench" / "results"
AUDIENCE = "money-planner-sync-proto"
USER = "00000000-0000-4000-8000-0000000e2e01"  # основной синтетический пользователь
BULK_USER = "00000000-0000-4000-8000-0000000e2e02"  # отдельный пользователь для первичной выгрузки


class MeteredTransport(HttpTransport):
    """HttpTransport, записывающий измерения каждого запроса в общий журнал под текущим шагом."""

    def __init__(self, client: httpx.AsyncClient, token: str, log: list[dict[str, Any]], device: str) -> None:
        super().__init__(client, token)
        self.log = log
        self.device = device
        self.step = ""

    async def _send(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        status: int | str = "error"
        try:
            out = await super()._send(method, url, **kw)
            status = 200
            return out
        except TransportError as e:
            status = str(e)
            raise
        finally:
            h = self.stats.last_headers
            self.log.append(
                {
                    "step": self.step,
                    "device": self.device,
                    "method": method,
                    "path": url,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "ru": int(h.get("x-ydb-ru", 0)),
                    "ydb_calls": int(h.get("x-ydb-calls", 0)),
                    "attempts": int(h.get("x-ydb-attempts", 1)),
                    "app_ms": float(h.get("server-timing", "app;dur=0").split("dur=")[1]),
                    "ru_formula": json.loads(h.get("x-ydb-stats", "{}")).get("ruIoFormula"),
                }
            )


class Run:
    def __init__(self, http: httpx.AsyncClient, secret: str, tmp: str) -> None:
        self.http, self.secret, self.tmp = http, secret, tmp
        self.log: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {}
        self.timings: dict[str, Any] = {}

    def device(self, name: str, user: str = USER, **kw: Any) -> SyncClient:
        token = auth.issue_test_token(self.secret, user, AUDIENCE)
        t = FaultyTransport(MeteredTransport(self.http, token, self.log, name))
        return SyncClient(str(Path(self.tmp) / f"{user}-{name}.sqlite"), name, t, **kw)

    @staticmethod
    def step(*devices: SyncClient, name: str) -> None:
        for d in devices:
            d.transport.inner.step = name  # type: ignore[attr-defined]

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = bool(ok)
        print(("OK   " if ok else "FAIL ") + name, flush=True)


# Повторять sync, пока не пройдёт (503 busy при троттлинге RU — штатная ситуация, push идемпотентен).
async def sync_until_ok(d: SyncClient, max_wait_s: float = 1800) -> tuple[int, float]:
    t0, tries, delay = time.perf_counter(), 0, 1.0
    while True:
        tries += 1
        if (await d.sync()).ok:
            return tries, time.perf_counter() - t0
        if time.perf_counter() - t0 > max_wait_s:
            raise RuntimeError(f"{d.device_id}: sync did not succeed in {max_wait_s} s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def scenario(run: Run, restore_n: int) -> None:
    http = run.http
    # 0. Защита: без токена push/pull закрыты, /health без токена — без подробностей.
    h = (await http.get("/health")).json()
    run.check("health без токена отдаёт только статус", h == {"status": "ok"})
    run.check("pull без токена → 401", (await http.get("/sync/pull")).status_code == 401)
    forged = auth.issue_test_token("x" * 48, USER, AUDIENCE)
    r = await http.get("/sync/pull", headers={"Authorization": f"Bearer {forged}"})
    run.check("чужой секрет → 401", r.status_code == 401)

    a, b = run.device("phone-a"), run.device("ipad-b")
    for d in (a, b):  # первый sync устройства (полная загрузка пустой истории)
        run.step(d, name="first_sync_empty")
        await sync_until_ok(d)

    # 1–2. Создание через push, получение через pull.
    run.step(a, b, name="create_push")
    cat = a.create_category("Продукты", "🛒")
    e1 = a.create_expense("250.50", cat, "2026-10-01", "синтетика: кофе")
    e2 = a.create_expense("1200.00", cat, "2026-10-02", "SUPERMARKET 0001 (импорт)")
    await sync_until_ok(a)
    run.step(a, b, name="pull_new")
    await sync_until_ok(b)
    run.check("B получил созданные A расходы", set(b.visible("expense")) == {e1, e2})

    # 3. Правка.
    run.step(a, b, name="edit")
    a.update("expense", e1, amount="260.00")
    await sync_until_ok(a)
    await sync_until_ok(b)
    run.check("правка дошла до B", b.visible("expense")[e1]["amount"] == "260.00")

    # 4. Удаление.
    run.step(a, b, name="delete")
    a.delete("expense", e2)
    await sync_until_ok(a)
    await sync_until_ok(b)
    run.check("удаление дошло до B (tombstone)", b.snapshot()[("expense", e2)] == (None, True))

    # 5. Два устройства правят один расход офлайн → одинаковый итог (позже по времени — B).
    run.step(a, b, name="conflict")
    a.update("expense", e1, comment="правка A")
    await asyncio.sleep(1)
    b.update("expense", e1, comment="правка B")
    await sync_until_ok(b)
    await sync_until_ok(a)
    await sync_until_ok(b)
    run.check(
        "конфликт двух устройств сошёлся к правке B",
        a.live() == b.live() and a.visible("expense")[e1]["comment"] == "правка B",
    )

    # 6. Повторная отправка: сервер сохранил, ответ потерян → повтор безопасен, без дублей.
    run.step(a, b, name="retry_lost_response")
    e3 = a.create_expense("99.00", cat, "2026-10-03", "повтор")
    a.transport.drop_push_responses = 1
    lost = await a.sync()
    retried = await a.sync()
    await sync_until_ok(b)
    run.check(
        "повтор после потерянного ответа: применено ровно один раз",
        not lost.ok and retried.ok and a.outbox_size() == 0 and list(b.visible("expense")).count(e3) == 1,
    )

    # 7. Архивирование категории синхронизируется.
    run.step(a, b, name="archive_category")
    other = a.create_category("Транспорт")
    await sync_until_ok(a)
    a.archive_category(other)
    await sync_until_ok(a)
    await sync_until_ok(b)
    run.check("архивирование категории дошло до B", b.visible("category")[other]["isArchived"] is True)

    # 8. Восстановление истории на новом устройстве.
    run.step(a, name="seed_history")
    for i in range(restore_n):
        a.create_expense(f"{i % 900 + 1}.00", cat, "2026-09-01", f"синтетика {i}")
    tries, secs = await sync_until_ok(a)
    run.timings["seed_history"] = {"records": restore_n, "sync_attempts": tries, "seconds": round(secs, 1)}
    c = run.device("new-phone-c", pull_limit=500)
    run.step(c, name="restore_new_device")
    tries, secs = await sync_until_ok(c)
    run.timings["restore_new_device"] = {"sync_attempts": tries, "seconds": round(secs, 1)}
    await sync_until_ok(a)
    await sync_until_ok(b)
    run.check("новое устройство восстановило всю историю", c.live() == a.live() == b.live())
    for d in (a, b, c):
        d.close()


# Первичная выгрузка большой истории (создание аккаунта с данными) при текущем лимите RU/с.
async def bulk_upload(run: Run, n: int) -> None:
    d = run.device("bulk-phone", user=BULK_USER, push_batch=500)
    run.step(d, name=f"bulk_upload_{n}")
    cat = d.create_category("Импорт")
    for i in range(n):
        d.create_expense(f"{i % 900 + 1}.00", cat, "2026-08-01", f"синтетика {i}")
    tries, secs = await sync_until_ok(d)
    busy = sum(1 for e in run.log if e["step"] == f"bulk_upload_{n}" and e["status"] != 200)
    run.timings["bulk_upload"] = {
        "records": n + 1,
        "sync_attempts": tries,
        "busy_503": busy,
        "seconds": round(secs, 1),
    }
    run.check(f"первичная выгрузка {n} записей завершилась", d.outbox_size() == 0)
    d.close()


def summarize(log: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for e in log:
        if e["status"] != 200:
            continue
        k = f"{e['step']} {e['method']} {e['path']}"
        s = out.setdefault(k, {"requests": 0, "ru": 0, "latency_ms": []})
        s["requests"] += 1
        s["ru"] += e["ru"]
        s["latency_ms"].append(e["latency_ms"])
    for s in out.values():
        lat = sorted(s.pop("latency_ms"))
        s["latency_ms_p50"] = lat[len(lat) // 2]
        s["latency_ms_max"] = lat[-1]
    return out


async def main(restore_n: int, bulk_n: int) -> None:
    url, secret = os.environ["GATEWAY_URL"], os.environ["SYNC_JWT_SECRET"]
    with tempfile.TemporaryDirectory() as tmp:
        async with httpx.AsyncClient(base_url=url, timeout=60) as http:
            run = Run(http, secret, tmp)
            await scenario(run, restore_n)
            if bulk_n:
                await bulk_upload(run, bulk_n)
    result = {
        "when": dt.datetime.now(dt.UTC).isoformat(),
        "checks": run.checks,
        "passed": sum(run.checks.values()),
        "total": len(run.checks),
        "timings": run.timings,
        "by_step": summarize(run.log),
        "requests": run.log,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "cloud_e2e.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"{result['passed']}/{result['total']} checks passed")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--restore", type=int, default=200, help="записей истории для восстановления")
    p.add_argument("--bulk", type=int, default=0, help="первичная выгрузка N записей (0 — пропустить)")
    a = p.parse_args()
    asyncio.run(main(a.restore, a.bulk))
