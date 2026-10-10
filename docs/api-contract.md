# Money Planner — контракт API v1 (этап E0)

Статус: **на утверждении** (E0 плана миграции, ред. 5.1). Продуктовая логика плана не меняется; технические уточнения, сделанные при описании контракта, перечислены в §12.

Машиночитаемый контракт — источник истины для backend, iOS и будущего Android:

| Файл | Что |
|---|---|
| [`contract/openapi.yaml`](../contract/openapi.yaml) | OpenAPI 3.1, публичные эндпоинты MVP (публикуются в API Gateway) |
| [`contract/openapi-internal.yaml`](../contract/openapi-internal.yaml) | `/internal/*` — фоновые задачи по таймеру, в шлюзе **не** публикуются |
| [`contract/system-categories.json`](../contract/system-categories.json) | реестр `SYSTEM_CATEGORY_IDS` (§3.2 плана) |
| [`contract/config-parameters.json`](../contract/config-parameters.json) | параметры конфигурации §6.3, константы протокола, параметры повторов клиента §8.1 |
| [`contract/vectors/comment.json`](../contract/vectors/comment.json) | тест-векторы правила комментария (§3.3) |
| [`contract/vectors/day-mark-id.json`](../contract/vectors/day-mark-id.json) | тест-векторы `entityId` отметки дня (§4) |
| [`contract/tests/`](../contract/tests) | контрактные тесты (без БД) |

Старый `openapi.json` в корне — контракт текущего кода на PostgreSQL. Он заменяется новым по мере этапов E1–E7 (план §11.2) и до этого проверяется как раньше (`scripts/openapi.py --check`).

---

## 1. Эндпоинты

| Метод и путь | Субъект | Успех | Этап |
|---|---|---|---|
| `GET /healthz` | — | 200 | E1 |
| `POST /v1/auth/guest` | — (`Idempotency-Key`) | 201 | E2 |
| `POST /v1/auth/refresh` | — (refresh-токен в теле) | 200 | E2 |
| `POST /v1/auth/apple` | гость | 200 | E5 |
| `POST /v1/auth/logout` | — (refresh-токен в теле) | 204 | E5 |
| `GET /v1/me` | гость или аккаунт | 200 | E4 |
| `DELETE /v1/account` | аккаунт | 202 | E7 |
| `GET /v1/account/deletions/{deletionId}` | гость (то же устройство) | 200 | E7 |
| `POST /v1/sync/push` | аккаунт | 200 | E6 |
| `GET /v1/sync/pull` | аккаунт | 200 | E6 |
| `POST /v1/imports/parse` | гость или аккаунт (`Idempotency-Key`) | 200 | E3, E4 |
| `POST /v1/imports/{importId}/displayed` | гость или аккаунт (то же устройство) | 200 | E4 |
| `POST /v1/apple/notifications` | — (JWS Apple) | 200 | E7 |
| `POST /internal/deletions/run`, `/internal/cleanup/*` | IAM таймер-триггера | 200 | E7, E8 |

Аналитических эндпоинтов нет: аналитика считается на устройстве (план §7).

## 2. Версионирование и совместимость

**Версия — в пути** (`/v1`, решение D17). `info.version` спецификации — semver контракта: minor — совместимые дополнения, major — новая версия пути.

**Совместимые изменения внутри `/v1`** (клиент обязан их переживать):
- новые эндпоинты;
- новые **необязательные** поля запроса; новые поля ответа;
- новые значения перечислений в ответах, помеченных `x-extensible-enum: true` (`ErrorCode`, `ImportUsage.state`, `DeletionState`, `AppleTokenRevocation`, `RejectReason`, коды `details[]`, `categoryStatus`, `ParseWarning.code`);
- новые коды ошибок с существующим HTTP-статусом;
- новые системные категории в реестре (сервер обновляется раньше клиентов).

**Несовместимые изменения** — только в `/v2` с периодом параллельной работы `/v1`: удаление или переименование полей, новые обязательные поля запроса, изменение типов и смысла, сужение допустимых значений, удаление id из реестра системных категорий.

**Правила клиента (iOS и Android одинаково):**
- неизвестные поля ответа игнорировать; неизвестное значение расширяемого перечисления — обрабатывать как «прочее» (например, неизвестный `code` ошибки — по HTTP-статусу и `retryable`);
- не разбирать JWT: срок жизни — из `accessTokenExpiresAt`;
- `deviceId`, `accountId`, `importId`, `deletionId` — непрозрачные UUID;
- платформа передаётся в `POST /v1/auth/guest` (`platform: ios | android`). Других различий в контракте между платформами нет: Android добавит только провайдера входа (новая строка `identities`, новый эндпоинт входа), остальной контракт общий.

**Версия формата данных sync** — `schemaVersion` у каждой мутации и записи. Сервер знает версии из `SYNC_KNOWN_SCHEMA_VERSIONS` (сейчас `[1]`); неизвестная версия в push → `426 upgrade_required` для всего запроса: клиент предлагает обновить приложение, outbox сохраняется.

**Старый API** (`/v1/auth/anonymous`, `/v1/expenses`, `/v1/categories`, `/v1/calendar`, `/v1/analytics`, `/v1/insights`, `DELETE /v1/me`) не поддерживается параллельно: выпущенных клиентов нет (план §11.2).

## 3. Субъекты и авторизация

| | Гость | Аккаунт |
|---|---|---|
| Как получить | `POST /v1/auth/guest` | `POST /v1/auth/apple` с гостевым токеном |
| Security scheme | `guestBearer` | `accountBearer` |
| Claims (для сервера) | `kind=guest`, `did` | `kind=account`, `sub=accountId`, `did` |
| Access JWT | 15 мин | 15 мин |
| Refresh | 365 дней, ротация, grace 60 с, reuse detection | то же |
| Доступно | `/me`, `/imports/*`, `/auth/apple`, статус удаления | `/me`, `/imports/*`, `/sync/*`, `DELETE /account` |

- Гостевой refresh-токен устройства хранится в Keychain и после входа в аккаунт: он нужен для импорта в состоянии `reauth_required`, статуса удаления и возврата в гостевой режим.
- Гостю `/sync/*` и `DELETE /account` → `403 account_required`.
- `userId` / `accountId` берутся только из токена; в теле и query их нет.

## 4. Формат ошибок

Единый конверт (модель `09 — Error Model`, без изменений формы):

```json
{ "error": { "code": "resync_required", "message": "Resync required", "status": 410,
             "retryable": false, "requestId": "req_…", "details": [] } }
```

- `code` — машинный код из каталога `x-error-catalog`; клиент ветвится только по нему.
- `message` — только для логов, пользователю не показывается, ПДн не содержит.
- `status` совпадает с HTTP-статусом; `requestId` совпадает с заголовком `X-Request-Id`.
- `retryable` — можно ли повторить **тот же** запрос автоматически (§5).
- `details[]` — `{ code, field?, index? }`, например `{ code: unknown_entity_type, field: entityType, index: 3 }`.

**Каталог кодов:**

| Код | HTTP | retryable | Retry-After |
|---|---|---|---|
| `invalid_request` | 400 | нет | |
| `apple_token_invalid` | 400 | нет | |
| `token_expired` | 401 | да, после `/auth/refresh` | |
| `access_token_invalid` | 401 | нет | |
| `refresh_token_invalid` | 401 | нет | |
| `session_revoked` | 401 | нет | |
| `account_deleted` | 401 | нет | |
| `account_required` | 403 | нет | |
| `not_found` | 404 | нет | |
| `import_not_found` | 404 | нет | |
| `deletion_not_found` | 404 | нет | |
| `method_not_allowed` | 405 | нет | |
| `idempotency_key_reused` | 409 | нет | |
| `idempotency_in_progress` | 409 | да | есть |
| `resync_required` | 410 | нет | |
| `delivery_expired` | 410 | нет | |
| `payload_too_large` | 413 | нет | |
| `file_too_large` | 413 | нет | |
| `unsupported_format` | 415 | нет | |
| `validation_failed` | 422 | нет | |
| `corrupted_file` | 422 | нет | |
| `unknown_bank` | 422 | нет | |
| `empty_statement` | 422 | нет | |
| `income_only` | 422 | нет | |
| `statement_too_large` | 422 | нет | |
| `upgrade_required` | 426 | нет | |
| `rate_limited` | 429 | да | есть |
| `unconfirmed_imports_limit` | 429 | да | есть |
| `internal_error` | 500 | да | |
| `processing_failed` | 500 | да | |
| `service_unavailable` | 503 | да | есть |
| `processing_timeout` | 503 | да | есть |

Общие с текущей моделью коды сохраняют статус и `retryable` (проверяется тестом). Коды старого API `access_token_expired`, `refresh_token_expired`, `user_deleted` заменены на `token_expired`, `refresh_token_invalid`, `account_deleted` (§2.8 плана).

**Коды 401 и действия клиента (§2.8 плана):**

| Код | Клиент |
|---|---|
| `token_expired` | `/auth/refresh`, повтор запроса |
| `access_token_invalid` | один `/auth/refresh` и повтор; если снова 401 — по коду ответа refresh |
| `refresh_token_invalid` | гость — новая гостевая запись; аккаунт — `reauth_required`, данные сохраняются |
| `session_revoked` | `reauth_required`, данные и outbox сохраняются |
| `account_deleted` | локальная очистка → гостевой режим |

## 5. Идемпотентность и повтор после сетевых ошибок

**Политика клиента (§8.1 плана, `contract/config-parameters.json → client`):**
- автоматически повторяются только: нет сети, таймаут, `5xx`, `429` и ответы с `retryable = true`;
- экранные операции (разбор, вход, удаление): задержка перед попыткой `n` — случайная в `[0; min(16 с, 2ⁿ⁻¹ с)]`, не больше 4 повторов, бюджет 45 с; `Retry-After` соблюдается, если укладывается в бюджет, иначе — сразу состояние ошибки с «Повторить»;
- фоновые операции (sync, очередь подтверждений импорта, `logout`) — тот же алгоритм с потолком 15 мин и без ограничения числа попыток;
- `4xx` кроме `429` (и `409 idempotency_in_progress`) не повторяются.

**Что безопасно повторять:**

| Эндпоинт | Механизм | Повтор после потерянного ответа |
|---|---|---|
| `POST /auth/guest` | `Idempotency-Key` (один на установку), окно 24 ч | тот же `deviceId` и `refreshToken`, `Idempotent-Replayed: true`; после использования токена — `409 idempotency_key_reused` |
| `POST /auth/refresh` | grace-окно 60 с | та же новая пара; позже — reuse → `401 refresh_token_invalid` |
| `POST /auth/apple` | одноразовые `nonce` и `authorizationCode` | безопасен, только если запрос не дошёл; иначе `400 apple_token_invalid` → новая авторизация Apple (новые `nonce` и код) |
| `POST /auth/logout` | по refresh-токену | всегда `204` |
| `GET /me`, `GET /sync/pull`, `GET /account/deletions/{id}` | чтение | безопасен |
| `POST /sync/push` | `mutationId` (журнал 30 дней) | тот же результат, `replayed = true`; тот же id с другим телом → `rejected/mutation_id_reused` |
| `POST /imports/parse` | `Idempotency-Key` попытки импорта + отпечаток файла (TTL 7 дней) | тот же `importId`, счётчик не растёт; категории могут отличаться (таблица §6.2 плана) |
| `POST /imports/{id}/displayed` | `importId` | no-op, тот же ответ |
| `DELETE /account` | задание по аккаунту | `202` с тем же `deletionId` |
| `POST /apple/notifications` | `jti` | `200`, без повторной обработки |

`Idempotency-Key` — UUID, генерируется клиентом; отсутствие или не-UUID → `400 invalid_request`. Ключ хранится вместе с операцией (установка; выбранный файл импорта) и не переиспользуется для другой операции.

## 6. Ограничения

| Что | Лимит | Ответ при превышении |
|---|---|---|
| Файл выписки | `IMPORT_MAX_FILE_BYTES` = 2 МБ (запрос через API Gateway — до 2,5 МБ с заголовками) | `413 file_too_large` |
| Операций в выписке | `IMPORT_MAX_OPERATIONS` = 5 000 | `422 statement_too_large` |
| Время разбора | `IMPORT_PARSE_TIMEOUT_S` = 30 с | `503 processing_timeout` + `Retry-After` |
| Частота разбора | 5 в минуту, 10 в сутки на устройство | `429 rate_limited` + `Retry-After` |
| Параллельный разбор | 1 на устройство / 4 на экземпляр | `429 rate_limited` / `503 service_unavailable` |
| Неподтверждённые результаты | 3 за 24 ч на устройство | `429 unconfirmed_imports_limit` + `Retry-After` |
| `categoryHints` | ≤ 500, `merchantKey` ≤ 40 символов | `422 validation_failed` |
| Создание гостевых записей | 10 в минуту на IP | `429 rate_limited` |
| Push | ≤ 500 мутаций и ≤ 1 МБ (клиент шлёт по 100) | `413 payload_too_large` |
| Pull | `limit` 1–500, по умолчанию 500 | `422 validation_failed` |
| Комментарий расхода | ≤ 200 Unicode scalar values | sync — `rejected/invalid`; импорт — обрезка по графемам |
| Название категории | 1–24 символа; эмодзи — 1 графема; `colorIndex` 0–11 | `rejected/invalid` |
| Сумма расхода | `0.01` … `999999999.99`, строка ровно с 2 знаками | `rejected/invalid` |
| Бесплатные импорты | `IMPORT_FREE_LIMIT` = 3, `ENFORCE_QUOTAS=false` | не блокируется, только `state` |

Параметры `env` меняются переменными окружения ревизии без релиза; константы `protocol` — только новой версией контракта.

## 7. Пагинация

Постраничная выдача в MVP одна — `GET /v1/sync/pull`, курсорная:
- `cursor` — последняя полученная версия (0 — с начала); `limit` ≤ 500; `horizon` — из прошлого ответа (0 — первый запрос);
- ответ `{ records, nextCursor, hasMore, horizon }`; записи по возрастанию `version`;
- клиент сохраняет страницу, `nextCursor` и `horizon` одной локальной транзакцией и продолжает, пока `hasMore = true`;
- курсор стабилен: вставки во время обхода получают большие версии и попадут на следующие страницы; повтор страницы идемпотентен;
- `410 resync_required` → полная загрузка с `cursor = 0` (возобновляемая, outbox сохраняется, план §8.4).

## 8. Sync: граница валидации

| Нарушение | Ответ |
|---|---|
| JSON не разобран | `400 invalid_request` |
| Нет `deviceId` / `mutations`, ошибка конверта мутации (`mutationId`, `entityType`, `entityId`, `op`, `baseVersion`, `createdAt`, `updatedAt`, `schemaVersion`) | `422 validation_failed` для всего запроса |
| Неизвестный `entityType` | `422 validation_failed`, `details[].code = unknown_entity_type` |
| Неизвестная `schemaVersion` | `426 upgrade_required` |
| > 500 мутаций или > 1 МБ | `413 payload_too_large` |
| payload и правила сущности: сумма, комментарий, категория (`kind`, реестр, смена `kind`, `op=delete`, «Другое»), отметка (`op=delete`, `entityId ↔ date`, дата в будущем), согласованность `op` / `deletedAt` / `payload` | `rejected`, `reason = invalid`, `details[]` — только эта мутация |

Известные коды `details[]` для `rejected/invalid`: `payload_invalid`, `op_not_allowed`, `comment_too_long`, `category_kind_mismatch`, `category_kind_change`, `system_category_unknown`, `fallback_category_not_archivable`, `day_mark_id_mismatch`, `date_in_future`.

Остальные правила протокола (delete wins, archive wins, LWW, перебазирование, `record` в ответе) — `docs/architecture-local-first.md` §8.2 без изменений.

## 9. Правила данных, общие для платформ

- **Комментарий** (§3.3 плана): NFC → удалить символы категории `Cc` → обрезать по краям символы категорий `Zs`/`Zl`/`Zp` (Python `str.strip()` после удаления `Cc`; Swift `.whitespacesAndNewlines`) → пустая строка = `null`. Длина — число Unicode scalar values. Импорт обрезает до наибольшего префикса из целых графем ≤ 200. Векторы — `contract/vectors/comment.json` (30 случаев, включая эмодзи, ZWJ, комбинируемые знаки, NFC до подсчёта).
- **`entityId` отметки дня** — `UUIDv5(NS_DAY_MARK, "YYYY-MM-DD")`, `NS_DAY_MARK = 97a5414e-31da-4b51-85c7-7ea3eab0c81d` (`x-contract.dayMarkNamespace`). Векторы — `contract/vectors/day-mark-id.json`.
- **Системные категории** — реестр `contract/system-categories.json`; «Другое» — `00000000-0000-4000-8000-000000000099`, не архивируется.
- **Даты** — календарные `YYYY-MM-DD`; время — RFC 3339 с часовым поясом; суммы — строка с 2 знаками.

## 10. Удаление аккаунта и уведомления Apple

- `DELETE /v1/account` → `202 { deletionId, state }`; с этого момента токен аккаунта получает `401 account_deleted` везде, кроме повтора `DELETE` (тот же `deletionId`).
- `GET /v1/account/deletions/{deletionId}` — только гостевым токеном устройства-инициатора; иначе `404 deletion_not_found`. Поле `appleTokenRevocation = skipped_no_token` — сигнал показать подсказку «отключите приложение в настройках Apple ID».
- `POST /v1/apple/notifications` — `200` после надёжной записи; `400` — подпись или claims неверны; `503` — Apple повторит.

## 11. Проверка контракта

```bash
python scripts/contract_spec.py --check   # OpenAPI 3.1, ссылки, дубликаты ключей, все примеры против схем
pytest contract/tests                     # смысловые проверки, без БД
```

В CI — задача `openapi` (без БД и без деплоя) и общий `pytest` в задаче `test`.

**Привязка кода к контракту (E1+).** Начиная с этапа, который реализует эндпоинт (`x-stage`), `scripts/openapi.py --check` сравнивает схему приложения с `contract/openapi.yaml` для реализованных операций, а тесты этапа проверяют ответы приложения против схем контракта (`contract_spec.schema_validator`). Изменение контракта — отдельным коммитом с обновлением этого документа.

## 12. Уточнения к плану, сделанные в E0 [Т]

Продуктовые решения не менялись. Чтобы контракт был полным, зафиксированы технические детали, которых не было в тексте плана:

1. **`POST /v1/auth/logout`** авторизуется самим refresh-токеном в теле, а не access JWT: best-effort повтор при следующем запуске работает и с истёкшим access JWT. Отзывается family токена; ответ всегда `204`.
2. **Повтор `DELETE /v1/account`** возвращает `202` с тем же `deletionId` (как в §2.5), хотя остальные запросы токеном аккаунта уже получают `401 account_deleted`.
3. **`appleTokenRevocation`** в статусе удаления — чтобы клиент узнал о `revoke_skipped_no_token` и показал подсказку (§2.5).
4. **`400 apple_token_invalid`** — общий код для невалидных `identityToken` / `authorizationCode` / повторного `nonce`; повтор `/auth/apple` после обработанного запроса требует новой авторизации Apple.
5. **`access_token_invalid`** сохранён из модели `09` для отсутствующего или повреждённого access JWT (в §2.8 его нет, так как он не зависит от состояния сессии).
6. **Граница валидации push** (§8): ошибки конверта мутации — `422` всего запроса; ошибки payload и правил сущности — `rejected/invalid` по мутации; > 500 мутаций — `413`, как и > 1 МБ.
7. **Сумма** в sync и импорте — строка ровно с 2 знаками после точки (`"250.50"`), без экспоненты и ведущих нулей.
8. **`NS_DAY_MARK`** — зафиксирован UUID пространства имён (в плане не был задан).
9. **Обрезка пробелов** в правиле комментария — символы категорий `Zs`/`Zl`/`Zp` (одинаково в Python и Swift).
10. **Коды 404** — `import_not_found` и `deletion_not_found` вместо общего `404`; чужие объекты не раскрываются.
11. **Пути `/internal/cleanup/*`** — имена задач E8 (в плане названа только `/internal/deletions/run`).

## 13. Открытые вопросы к владельцу продукта

1. **Состав системных категорий.** Реестр взят из seed `migrations/versions/0002_system_categories.py`, помеченного «черновик списка, согласовать». UUID после релиза менять и удалять нельзя — нужен подтверждённый список.
2. **`categoryStatus = unassigned` и `needs_review`.** План называет состояние «без категории, на проверку» `needs_review`, текущий код и контракт — `unassigned` (вместе с `assigned` / `suggested`). В контракте оставлено существующее значение `unassigned`; переименовать до E3 можно без затрат.
3. **Вход тем же Apple ID во время незавершённого удаления** (`users.state = deleting`). План это не описывает: варианты — `409` с `Retry-After` до завершения удаления или ожидание в запросе. Нужно решение до E5/E7; в контракт пока не внесено.
