"""Облачные измерения холодного/тёплого старта (запускать только после cloud/deploy.sh).

    export GATEWAY_URL=https://<domain>  SYNC_JWT_SECRET=$(yc lockbox payload get ...)
    python -m cloud.measure --idle 1,5,15 --history 200

Для каждого периода простоя: ждём, затем первый запрос (/health с токеном) → признак холодного
экземпляра (`requestsServedByInstance == 1`), время старта процесса, драйвера YDB и первого
запроса к YDB; сразу после — push 8, pull 10 и восстановление истории (новый телефон).
История небольшая (по умолчанию 200 записей), чтобы не упираться в лимит RU/с по умолчанию:
первичную выгрузку большой истории отдельно проверяет cloud.e2e --bulk.
Затем 20 тёплых повторов. Результат: bench/results/cloud_coldstart.json.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import time
import uuid
from pathlib import Path

import httpx

from bench.bench import Client, expense
from syncproto import auth

# Результаты пишутся рядом с локальными, в bench/results/.
RESULTS = Path(__file__).resolve().parents[1] / "bench" / "results"
USER = "00000000-0000-4000-8000-0000000c1000"  # фиксированный синтетический пользователь


# Один запрос /health с токеном: время ответа и признаки холодного экземпляра.
async def probe(http: httpx.AsyncClient, token: str) -> dict:
    t0 = time.perf_counter()
    r = await http.get("/health", headers={"Authorization": f"Bearer {token}"})
    total = (time.perf_counter() - t0) * 1000
    h = r.json()
    return {
        "health_ms": round(total, 1),
        "cold": h.get("requestsServedByInstance") == 1,
        "startup": h.get("startup"),
        "maxRssMb": h.get("maxRssMb"),
    }


# Подготовить историю 1000 записей, затем для каждого периода простоя — холодный замер, потом тёплые.
# Push с повтором при 503 (троттлинг RU/с): push идемпотентен по mutationId.
async def _push_retry(c: Client, muts: list[dict]) -> dict:
    for delay in (1, 2, 4, 8, 16, 30, 30, 30):
        try:
            _, meas = await c.push(muts)
            return meas
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 503:
                raise
            await asyncio.sleep(delay)
    raise RuntimeError("push throttled for too long")


async def main(idles: list[float], warm: int, history: int) -> None:
    url, secret = os.environ["GATEWAY_URL"], os.environ["SYNC_JWT_SECRET"]
    token = auth.issue_test_token(secret, USER, "money-planner-sync-proto")
    out = {"when": dt.datetime.now(dt.UTC).isoformat(), "history": history, "cold": [], "warm": []}
    async with httpx.AsyncClient(base_url=url, timeout=60) as http:
        c = Client(http, token, "cloud-probe")
        for off in range(0, history, 100):  # история для восстановления, пачками по 100
            await _push_retry(c, [expense(off + j) for j in range(min(100, history - off))])
        for idle in idles:
            print(f"idle {idle} min...", flush=True)
            await asyncio.sleep(idle * 60)
            p = await probe(http, token)
            _, push = await c.push([expense(i) for i in range(8)])
            _, pull = await c.pull(0, 10)
            t0 = time.perf_counter()
            cursor, pages = 0, 0
            fresh = Client(http, token, f"new-phone-{uuid.uuid4().hex[:6]}")
            while True:
                page, _ = await fresh.pull(cursor, 500)
                pages += 1
                cursor = page["nextCursor"]
                if not page["hasMore"]:
                    break
            restore_ms = (time.perf_counter() - t0) * 1000
            out["cold"].append(
                {
                    "idle_min": idle,
                    **p,
                    "push8": push,
                    "pull10": pull,
                    "restore_ms": round(restore_ms, 1),
                    "restore_pages": pages,
                }
            )
            print(out["cold"][-1], flush=True)
        for _ in range(warm):
            p = await probe(http, token)
            _, push = await c.push([expense(i) for i in range(8)])
            _, pull = await c.pull(0, 10)
            out["warm"].append(
                {
                    **p,
                    "push8_ms": push["latency_ms"],
                    "pull10_ms": pull["latency_ms"],
                    "push8_ru": push["ru_header"],
                    "push8_ru_formula": push["ru_formula"],
                }
            )
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "cloud_coldstart.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--idle", default="1,5,15")
    a.add_argument("--warm", type=int, default=20)
    a.add_argument("--history", type=int, default=200)
    args = a.parse_args()
    asyncio.run(main([float(x) for x in args.idle.split(",")], args.warm, args.history))
