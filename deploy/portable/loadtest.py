"""Нагрузочная проверка тестового стенда. Только синтетические данные и stdlib (запуск на сервере).

    python3 deploy/portable/loadtest.py tests/fixtures/banks/alfa/alfa_statement_test.xlsx <large.xlsx>

<large.xlsx> — синтетическая выписка из scripts/make_synthetic_statement.py.
Фазы: одиночный импорт (разбор + сохранение), 4 и 8 одновременных импортов, аналитика,
смешанная нагрузка API. Строки «@@ <unix-time> <фаза>» — для сопоставления с vmstat/docker stats.
Создаёт 8 гостевых пользователей с синтетическими расходами; production-данные не трогает.
"""

import concurrent.futures as cf
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid

B = "http://127.0.0.1:8000"
PRODUCTS = "00000000-0000-4000-8000-000000000001"
SMALL, LARGE = (open(p, "rb").read() for p in sys.argv[1:3])
errors: list[str] = []


def mark(phase):
    print(f"@@ {time.time():.0f} {phase}", flush=True)


def req(method, path, headers=None, js=None, data=None, retry_429=True):
    h = {"X-Timezone": "Europe/Moscow", **(headers or {})}
    if js is not None:
        data = json.dumps(js).encode()
        h["Content-Type"] = "application/json"
    waited = 0.0
    t = time.perf_counter()
    while True:
        try:
            r = urllib.request.Request(B + path, data=data, method=method, headers=h)
            with urllib.request.urlopen(r, timeout=120) as resp:
                ms = (time.perf_counter() - t) * 1000
                return resp.status, json.loads(resp.read() or b"null"), ms, waited
        except urllib.error.HTTPError as e:
            if e.code == 429 and retry_429:
                ra = float(e.headers.get("Retry-After", "5"))
                waited += ra
                time.sleep(ra)
                continue
            errors.append(f"{method} {path} {e.code} {e.read()[:200]!r}")
            return e.code, None, (time.perf_counter() - t) * 1000, waited


def session():
    _, body, _, _ = req(
        "POST",
        "/v1/auth/anonymous",
        {"Idempotency-Key": str(uuid.uuid4())},
        {"platform": "ios", "appVersion": "bench"},
    )
    return {"Authorization": "Bearer " + body["accessToken"]}


def multipart(data):
    bnd = "b" + uuid.uuid4().hex
    body = (
        (
            f'--{bnd}\r\nContent-Disposition: form-data; name="file"; filename="s.xlsx"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{bnd}--\r\n".encode()
    )
    return body, {"Content-Type": f"multipart/form-data; boundary={bnd}"}


def one_import(auth, data):
    """Разбор выписки + сохранение результата (как iOS после проверки черновика)."""
    body, h = multipart(data)
    c1, parsed, t_parse, w1 = req("POST", "/v1/imports/parse", {**auth, **h}, data=body)
    if c1 != 200:
        return {"ok": False, "parse_ms": t_parse}
    ops = parsed["operations"]
    expenses = [
        {
            "date": o["date"],
            "amount": o["amount"],
            "categoryId": PRODUCTS,
            "comment": (o.get("description") or "")[:200] or None,
        }
        for o in ops
    ]
    c2, saved, t_save, w2 = req(
        "POST", "/v1/expenses/import", {**auth, "Idempotency-Key": str(uuid.uuid4())}, {"expenses": expenses}
    )
    return {
        "ok": c2 == 201,
        "ops": len(ops),
        "imported": (saved or {}).get("importedCount"),
        "parse_ms": t_parse,
        "save_ms": t_save,
        "waited_429_s": w1 + w2,
    }


def show(name, results, wall):
    ok = [r for r in results if r["ok"]]
    p = [r["parse_ms"] for r in ok]
    s = [r["save_ms"] for r in ok]
    print(
        f"{name}: ok={len(ok)}/{len(results)} ops/import={ok[0]['ops'] if ok else 0} "
        f"imported={sum(r['imported'] or 0 for r in ok)} wall={wall:.1f}s "
        f"parse p50={statistics.median(p):.0f} max={max(p):.0f}ms "
        f"save p50={statistics.median(s):.0f} max={max(s):.0f}ms "
        f"waited_on_429={sum(r['waited_429_s'] for r in results):.0f}s",
        flush=True,
    )


auths = [session() for _ in range(8)]  # лимит 10 гостевых сессий в минуту на IP

for name, data, n in [
    ("single small (120 ops)", SMALL, 1),
    ("single large", LARGE, 1),
    ("4 concurrent large", LARGE, 4),
    ("8 concurrent large", LARGE, 8),
]:
    mark(name)
    t = time.perf_counter()
    with cf.ThreadPoolExecutor(n) as ex:
        res = list(ex.map(one_import, auths[:n], [data] * n))
    show(name, res, time.perf_counter() - t)
    time.sleep(5)

mark("analytics on ~19k rows")
c, body, ms, _ = req("GET", "/v1/analytics/summary?from=2024-04-01&to=2024-04-30", auths[0])
print(f"analytics: {c} {ms:.0f}ms total={body and body.get('total')}", flush=True)

mark("mixed api 600 req / 8 threads")


def work(i):
    a = auths[i % 8]
    if i % 3 == 0:
        return req(
            "POST",
            "/v1/expenses",
            {**a, "Idempotency-Key": str(uuid.uuid4())},
            {"date": f"2026-09-{i % 28 + 1:02d}", "amount": f"{i + 1}.00", "categoryId": PRODUCTS},
        )
    if i % 3 == 1:
        return req("GET", "/v1/expenses?from=2026-09-01&to=2026-09-30", a)
    return req("GET", "/v1/analytics/summary?from=2026-09-01&to=2026-09-30", a)


t = time.perf_counter()
with cf.ThreadPoolExecutor(8) as ex:
    res = list(ex.map(work, range(600)))
el = time.perf_counter() - t
lat = sorted(r[2] for r in res)
p95 = lat[int(0.95 * len(lat))]
print(
    f"mixed: 600 req in {el:.1f}s = {600 / el:.0f} rps, non-2xx={sum(r[0] >= 300 for r in res)}, "
    f"p50={statistics.median(lat):.0f}ms p95={p95:.0f}ms max={lat[-1]:.0f}ms",
    flush=True,
)
mark("end")
print("errors:", len(errors), *errors[:10], sep="\n  ")
