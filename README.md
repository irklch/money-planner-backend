# Money Planner — backend

Модульный монолит: FastAPI, Pydantic v2, async SQLAlchemy 2.0, Alembic, PostgreSQL 16. API живёт под префиксом `/v1`, контракт для клиентов лежит в [`openapi.json`](openapi.json).

> **Переход на local-first (план [`docs/production-migration-plan.md`](docs/production-migration-plan.md)).** Новый контракт API v1 для iOS и Android — [`contract/openapi.yaml`](contract/openapi.yaml), правила — [`docs/api-contract.md`](docs/api-contract.md) (этап E0). Текущий код и `openapi.json` заменяются новым контрактом по этапам E1–E7.

Источники истины, по порядку приоритета:
1. Figma «Money Planner — API Architecture», страница **11 — Database Schema**.
2. Страницы **02–10** того же файла: контракты, модель ошибок, финальные решения.
3. Технические решения этапа реализации.

## Запуск

```bash
docker compose up -d db
docker compose run --rm tests            # pytest против реального PostgreSQL 16
docker compose up --build api            # http://localhost:8000/docs
```

Локально без Docker нужен Python 3.12: `pip install . --group dev`, затем `pytest`.

## Структура

```
app/
  core/        конфиг, ошибки (09 — Error Model), логи без ПДн, rate limit, JWT, Lockbox
  db/          ORM-модели строго по схеме: users, refresh_tokens, categories, expenses, free_days, expense_imports
  ai/          LLMClient (свой интерфейс) + адаптер Yandex AI Studio
  modules/
    auth/        POST /auth/anonymous, /auth/refresh — гостевая сессия, ротация, reuse detection
    account/     DELETE /me
    categories/  CRUD, архивирование вместо удаления
    expenses/    CRUD + POST /expenses/import (атомарно, идемпотентно через expense_imports)
    calendar/    «Бесплатные дни» (day acknowledgements), инвариант Expense XOR отметка
    imports/     POST /imports/parse — чтение в памяти, реестр банков, маскирование ПДн, категоризация
    analytics/   summary, dynamics — средние по учтённым дням
    insights/    AI-выводы по готовым фактам с проверкой чисел
  jobs/        purge_inactive_users (12 мес), check_ai
migrations/    0001 — схема, 0002 — системные категории (предварительный seed), 0003 — результат импорта
scripts/       openapi.py — экспорт и --check контракта кода; contract_spec.py — проверка contract/
contract/      контракт API v1 (OpenAPI 3.1), реестр системных категорий, лимиты, тест-векторы, контрактные тесты
deploy/        Yandex Cloud: COI compose, Caddy, инструкция
```

## Как реализованы ключевые правила

| Правило | Где и как |
|---|---|
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
