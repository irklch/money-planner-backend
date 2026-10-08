# Прототип: sync Money Planner на YDB Serverless

Исследовательский прототип, изолированный от production-кода (`app/` не импортируется и не меняется). Итоги — в [REPORT.md](REPORT.md).

## Состав

```
syncproto/        сервер
  models.py       контракт API (camelCase) и внутренние структуры
  resolve.py      правила конфликтов: version (B, выбран), hlc / hlc_dw (A), lww_clock (базовая линия)
  hlc.py          Hybrid Logical Clock для варианта A
  engine.py       алгоритм push/pull, чистое планирование пачки
  store_ydb.py    YDB: одна serializable-транзакция на push, snapshot-чтение на pull
  store_memory.py эталонное хранилище в памяти для симуляции
  schema.py       DDL: sync_records (+ GLOBAL SYNC индекс by_version), sync_state, sync_mutations (TTL 30 дней)
  metering.py     перехват x-ydb-consumed-units и подсчёт обращений к YDB
  auth.py         JWT: userId только из подписанного токена
  api.py          FastAPI: /health, POST /sync/push, GET /sync/pull
client/           тестовый клиент: SQLite + персистентный outbox, транспорт со сбоями, симуляция
tests/            сценарии (15 обязательных + регрессии), правила, симуляция, свойства YDB
bench/            benchmark, модель стоимости, результаты (bench/results/)
cloud/            облачный этап: deploy/teardown, API Gateway, замер холодного старта — НЕ запускался
```

## Локальный запуск

Нужны Docker и Python 3.12. На Apple Silicon официальный образ `local-ydb` (только amd64) работает через Rosetta: `colima start --vz-rosetta`.

```bash
cd prototype/ydb-sync
docker compose up -d ydb                      # YDB на 127.0.0.1:2136, без аутентификации — только локально
uv venv --python 3.12 .venv && uv pip install --python .venv -e . --group dev   # или pip install . --group dev
.venv/bin/pytest -q                           # 60 тестов, ~30 с
.venv/bin/python -m client.simulate --seeds 1000   # сравнение стратегий, ~1 мин
.venv/bin/python -m bench.bench -n 30         # benchmark → bench/results/local_benchmark.json
.venv/bin/python -m bench.cost_model          # → bench/results/cost_model.md
```

API в контейнере:

```bash
export SYNC_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
.venv/bin/python -m syncproto.schema create
docker compose up -d --build api              # http://127.0.0.1:8080/health
.venv/bin/python -m bench.bench --target url --url http://127.0.0.1:8080 --out container_local_benchmark.json
```

## Протокол (кратко)

`POST /sync/push` — `Authorization: Bearer <JWT>`:

```json
{"deviceId": "iphone-1", "mutations": [{
  "mutationId": "uuid", "entityType": "expense", "entityId": "uuid", "op": "upsert",
  "baseVersion": 41, "createdAt": "...", "updatedAt": "...", "deletedAt": null,
  "schemaVersion": 1, "payload": {"amount": "250.50", "categoryId": "uuid", "date": "2026-10-01", "comment": "кофе"}}]}
```

Ответ — результат каждой мутации: `applied` (+ `version`), `rejected` (`conflict` | `deleted` | `mutation_id_reused`) или `noop`. Флаги `conflict` и `replayed` и поле `record` с актуальной серверной записью, когда она отличается от клиентской.

`GET /sync/pull?cursor=N&limit=500` возвращает `{records, nextCursor, hasMore}`, удаления приходят как tombstone (`deletedAt`). При курсоре старше горизонта очистки tombstones ответ — `410 resync_required`.

Ограничения: 500 мутаций и 1 МБ на push, 500 записей на страницу pull. При исчерпании повторов транзакции — `503` + `Retry-After` (push идемпотентен).

## Безопасность

- Все данные синтетические, пользователи и устройства — фиксированные тестовые UUID.
- Секретов в репозитории нет: JWT-секрет генерируется на запуск, в облаке хранится в Lockbox.
- `userId` берётся только из подписанного токена, лишние поля в теле запрещены (`422`), чужой секрет даёт `401`.
- Локальная YDB опубликована только на `127.0.0.1`. Анонимный доступ к YDB запрещён вне локальной среды (`ENV=cloud` требует IAM и `grpcs://`).
