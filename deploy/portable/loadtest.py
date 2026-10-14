"""Нагрузочная проверка тестового стенда. Только синтетические данные и stdlib (запуск на сервере).

    python3 deploy/portable/loadtest.py tests/fixtures/banks/alfa/alfa_statement_test.xlsx <large.xlsx>

<large.xlsx> — синтетическая выписка из scripts/make_synthetic_statement.py.
Фазы (возможности E1): одиночный разбор малой и большой выписки, 4 и 8 одновременных разборов,
600 запросов /healthz с проверкой БД. Строки «@@ <unix-time> <фаза>» — для сопоставления
с vmstat и docker stats.
Создаёт 8 гостевых записей (старый /v1/auth/anonymous до E2) без финансовых данных.
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


def one_parse(auth, data):
    """Разбор выписки. Сохранения на сервере в MVP нет: черновик и расходы уходят через sync (E6)."""
    body, h = multipart(data)
    c, parsed, ms, waited = req("POST", "/v1/imports/parse", {**auth, **h}, data=body)
    return {
        "ok": c == 200,
        "ops": len(parsed["operations"]) if c == 200 else 0,
        "parse_ms": ms,
        "waited": waited,
    }


def show(name, results, wall):
    ok = [r for r in results if r["ok"]]
    p = [r["parse_ms"] for r in ok] or [0]
    print(
        f"{name}: ok={len(ok)}/{len(results)} ops/parse={ok[0]['ops'] if ok else 0} wall={wall:.1f}s "
        f"parse p50={statistics.median(p):.0f} max={max(p):.0f}ms "
        f"waited_on_429={sum(r['waited'] for r in results):.0f}s",
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
        res = list(ex.map(one_parse, auths[:n], [data] * n))
    show(name, res, time.perf_counter() - t)
    time.sleep(5)

# Смешанная нагрузка E1: /healthz с запросом к БД (SELECT 1 через пул). Маршруты /v1/auth/* ограничены
# частотой по IP и для неё не подходят; нагрузка на sync — с E6.
mark("healthz 600 req / 8 threads")
t = time.perf_counter()
with cf.ThreadPoolExecutor(8) as ex:
    res = list(ex.map(lambda _: req("GET", "/healthz"), range(600)))
el = time.perf_counter() - t
lat = sorted(r[2] for r in res)
p95 = lat[int(0.95 * len(lat))]
print(
    f"healthz: 600 req in {el:.1f}s = {600 / el:.0f} rps, non-2xx={sum(r[0] >= 300 for r in res)}, "
    f"p50={statistics.median(lat):.0f}ms p95={p95:.0f}ms max={lat[-1]:.0f}ms",
    flush=True,
)
mark("end")
print("errors:", len(errors), *errors[:10], sep="\n  ")
