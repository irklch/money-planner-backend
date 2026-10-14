# Money Planner — backend

Модульный монолит: FastAPI, Pydantic v2, async SQLAlchemy 2.0, Alembic, PostgreSQL 16. API живёт под префиксом `/v1`, контракт для клиентов лежит в [`openapi.json`](openapi.json).

> **Переход на local-first (план [`docs/production-migration-plan.md`](docs/production-migration-plan.md), ред. 6, D24: ВМ + Docker Compose + PostgreSQL).** Контракт API v1 для iOS и Android — [`contract/openapi.yaml`](contract/openapi.yaml), правила — [`docs/api-contract.md`](docs/api-contract.md). **Готово: E0 (контракт), E1 (каркас и схема MVP на PostgreSQL).** Старые `/v1/auth/*`, `DELETE /v1/me` и `/v1/imports/parse` работают до замены в E2–E7; модули, ушедшие на iOS, отключены. Статус этапов — план §11.0, результат E1 — §11.1c.

Источники истины, по порядку приоритета:
1. Figma «Money Planner — API Architecture», страница **11 — Database Schema**.
2. Страницы **02–10** того же файла: контракты, модель ошибок, финальные решения.
3. Технические решения этапа реализации.

## Запуск

```bash
docker compose up -d db
docker compose run --rm tests            # pytest против реального PostgreSQL 16 (схема, миграции, API, контракт)
docker compose up --build api            # alembic upgrade head + API: http://localhost:8000/docs, /healthz
```

Локально без Docker нужен Python 3.12: `pip install . --group dev`, затем `pytest`. Конфигурация — [`.env.example`](.env.example): `DATABASE_URL` целиком или части `DB_*`, параметры §6.3.

Миграции: `python -m alembic upgrade head` (чистая БД и переход с 0003 — одинаково), `python -m alembic check` (модели = схема). Сервер, резервные копии и учения восстановления — [`deploy/portable`](deploy/portable/README.md).

## Структура

```
app/
  core/        конфиг (параметры §6.3), ошибки (09 — Error Model), логи без ПДн, rate limit, JWT
  db/          schema.py — схема MVP (15 таблиц, E1); models.py — старая схема (legacy_users, legacy_refresh_tokens,
               categories, expenses, free_days, expense_imports) до E11; session.py — пул, ping для /healthz
  ai/          LLMClient (свой интерфейс) + адаптер Yandex AI Studio
  modules/     смонтированы:
    auth/        POST /auth/anonymous, /auth/refresh — до E2 (/auth/guest)
    account/     DELETE /me — до E7 (DELETE /account)
    imports/     POST /imports/parse — до E3: чтение в памяти, реестр банков, маскирование ПДн, категоризация
               отключены в E1 (логика на iOS или вне MVP), код удаляется в E11:
    categories/, expenses/, calendar/, analytics/, insights/
  jobs/        purge_inactive_users (12 мес), check_ai, fence_check (проверка домена при старте)
migrations/    0001 — схема, 0002 — системные категории, 0003 — результат импорта, 0004 — схема MVP (E1)
scripts/       openapi.py — экспорт и --check контракта кода; contract_spec.py — проверка contract/
contract/      контракт API v1 (OpenAPI 3.1), реестр системных категорий, лимиты, тест-векторы, контрактные тесты
deploy/        portable/ — любой сервер с Docker, копии БД вне облака; Caddyfile
docs/          disaster-recovery.md — аварийный план; infra-audit-2026-10-11.md — состояние инфраструктуры
```

## Как реализованы ключевые правила

Строки про `POST /expenses`, `/categories`, импорт через `expense_imports`, «Бесплатный день» и AI-выводы описывают код, **отключённый в E1** (логика на iOS или вне MVP); он остаётся в репозитории до E11.

| Правило | Где и как |
|---|---|
| `/healthz` и недоступная БД (E1) | `SELECT 1` через пул с таймаутом `DB_HEALTH_TIMEOUT_S`. БД недоступна — `503 service_unavailable` + `Retry-After` (`DB_UNAVAILABLE_RETRY_AFTER_S`) на `/healthz` и любом маршруте с БД; без текста драйвера, адреса и секретов в ответе и логе |
| Идемпотентность импорта | `expense_imports.id = Idempotency-Key` + `request_hash` (SHA-256 нормализованного тела) + сохранённый результат (`imported_count`, `date_from`, `date_to`). Всё пишется одной транзакцией с расходами. То же тело → сохранённый результат; другое тело → `409 idempotency_key_reused`; параллельный запрос → `409 idempotency_in_progress` + `Retry-After`. |
| Идемпотентность `POST /expenses` и `/categories` | Idempotency-Key становится id создаваемой записи, поэтому дополнительная таблица не нужна. Ключ другого пользователя даёт `409 idempotency_key_reused`. |
| Повтор `POST /auth/anonymous` | refresh-токен = HMAC(`TOKEN_DERIVATION_SECRET`, ключ). В БД хранится только SHA-256 токена. Повтор пересчитывает токен из ключа и отдаёт его, только пока токен не ротирован, не отозван и не истёк, в пределах 24 ч. Иначе `409 idempotency_key_reused` без токенов. См. `docs/contract-changes.md`. |
| Grace-окно refresh (60 с) | Новый токен детерминированно выводится из старого, поэтому повтор старого токена в окне отдаёт ту же новую пару. Вне окна это считается reuse и отзывается вся family. |
| Expense XOR «Бесплатный день» | Любая запись расхода снимает отметку в своей транзакции. PUT отметки на дату с расходами даёт 409. Гонку исключают advisory-локи по (user, date). |
| «Сегодня» | iOS передаёт IANA-пояс в `X-Timezone`, сервер проверяет `date ≤ сегодня` в этом поясе. Даты остаются календарными. |
| Изоляция пользователей | Все запросы фильтруются по `user_id` из JWT. Чужие объекты получают 404, а не 403. Само знание UUID не даёт доступа. |
| Выписка не сохраняется | Multipart разбирается в памяти: `UploadFile`/`SpooledTemporaryFile` не используются, потому что пишут на диск файлы больше 1 МБ. В проде корневая ФС контейнера read-only. |
| ПДн и ИИ | В LLM уходят только маскированные названия операций (без карт, счетов, телефонов, ФИО, email) и названия категорий. Заголовок `x-data-logging-enabled: false` передаётся всегда. |
| Числа в AI-выводах | Backend считает все суммы, доли и изменения. Вывод, в котором есть число, отсутствующее в фактах, отбрасывается. |
| Логи | JSON с белым списком полей: метод, шаблон маршрута, статус, длительность, requestId. Query string, тела, суммы и токены в логи не пишутся. |

## Банки

Реестр адаптеров: `app/modules/imports/parsing/registry.py`. Поддерживаются только варианты выписок, проверенные на образце. Остальные файлы получают `422 unknown_bank`.

| Банк | code | Формат | Образец |
|---|---|---|---|
| Альфа-Банк | `alfa` | `.xlsx` «Выписка по счету», счёт в рублях | `tests/fixtures/banks/alfa/` |

CSV и другие Excel-варианты Альфа-Банка **не поддерживаются**, пока для них нет образца.

### Альфа-Банк

Ограничения: только `.xlsx` «Выписка по счету» и только **рублёвые** счета с явно указанной валютой (RUR/RUB). Если валюты нет, поле пустое или валюта другая, выписка получает `422 unknown_bank`. Правила первой версии и сверка образца описаны в [docs/banks/alfa.md](docs/banks/alfa.md). Кратко:
- дата расхода — дата операции; дата проводки нужна только для проверки завершённости;
- в расчёт идут «Выполнен» и пустой статус при дате проводки, остальное даёт `partially_read`;
- переводы между своими счетами исключаются в обе стороны и не входят в `skippedIncomeCount`, но только при подтверждённом совпадении ФИО с полем «Клиент» шапки; без него перевод остаётся обычной операцией;
- СБП C2B и переводы физлицам по СБП остаются расходами на проверку;
- поступления пропускаются, возвраты не угадываются;
- типы операций банка не используются как категории.

Добавление банка:
1. Положить анонимизированный образец в `tests/fixtures/banks/<code>/`.
2. Описать колонки в наследнике `TabularAdapter`, заявить проверенные `formats`.
3. Добавить тесты на образце (с независимым расчётом сумм) и только потом зарегистрировать адаптер.
