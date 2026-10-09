# Облачный этап — требует подтверждения

Ничего из этого не запускалось. `deploy.sh` создаёт платные ресурсы и без `CONFIRM_PAID_RESOURCES=yes` сразу завершается.

## Что создаётся

| Ресурс | Параметры | Стоимость за 1–2 дня эксперимента |
|---|---|---|
| YDB Serverless `mp-sync-proto-db` | лимит 10 RU/с (по умолчанию), без выделенной ёмкости, 1 ГБ | 0 ₽: прогон ≈ 0,2–0,4 млн RU при бесплатном 1 млн/мес |
| Serverless Container `mp-sync-proto-api` | 1 vCPU, 512 МБ, concurrency 4, timeout 30 с, **0 подготовленных экземпляров** | 0 ₽: бесплатно 5 vCPU·ч, 10 ГБ·ч, 1 млн вызовов |
| API Gateway `mp-sync-proto-gw` | 3 маршрута | 0 ₽: бесплатно 100 тыс. запросов |
| Lockbox `mp-sync-proto-jwt` | 1 версия секрета | ~20 ₽/мес, за 2 дня ≈ 1–2 ₽ |
| Container Registry | 1 образ ~83 МБ | < 1 ₽ |
| 2 сервисных аккаунта | — | 0 ₽ |
| Cloud Logging `mp-sync-proto-logs` | хранение 1 день, уровень warn | 0 ₽ |

Итого **≈ 2–3 ₽**, если удалить ресурсы после измерений (`teardown.sh`). Цены взяты из architecture-local-first.md. Перед запуском стоит завести бюджет с уведомлением в биллинге, например на 100 ₽.

## Минимальные IAM-роли

| Кто | Роль | На что |
|---|---|---|
| `mp-sync-proto-runtime` (сервисный аккаунт ревизии контейнера) | `ydb.editor` | только на БД `mp-sync-proto-db` |
| | `lockbox.payloadViewer` | только на секрет `mp-sync-proto-jwt` |
| | `container-registry.images.puller` | только на реестр прототипа |
| `mp-sync-proto-gateway` (сервисный аккаунт шлюза) | `serverless.containers.invoker` | только на контейнер `mp-sync-proto-api` |
| Оператор (вы) | `editor` на каталог или набор `ydb.admin`, `serverless.containers.editor`, `api-gateway.editor`, `lockbox.editor`, `iam.serviceAccounts.user` | на время развёртывания |

Безопасный доступ к YDB:
- контейнер получает IAM-токен из сервиса метаданных (`YDB_AUTH=metadata`), поэтому ключей и паролей нет ни в образе, ни в переменных;
- YDB Serverless принимает только аутентифицированные gRPC-запросы по TLS (`grpcs://`). Анонимный режим в `ENV=cloud` запрещён конфигурацией;
- схему создаёт оператор своим IAM-токеном (`YDB_AUTH=env`), у контейнера нет прав на DDL сверх `ydb.editor`. В production DDL стоит вынести в отдельный сервисный аккаунт CI с `ydb.admin`, а runtime оставить с правами на данные.

Защита endpoint:
- у контейнера нет `allUsers` invoker, вызвать его напрямую нельзя;
- наружу через шлюз открыты только `/health`, `/sync/push`, `/sync/pull`;
- push и pull требуют JWT, подписанного секретом из Lockbox. `userId` берётся только из токена;
- `/health` без токена отвечает только `{"status":"ok"}`.

Значение секрета передаётся в `yc lockbox secret create` через stdin и не попадает в аргументы процессов.

## Шаги

1. Вы подтверждаете запуск и указываете `FOLDER_ID`.
2. `CONFIRM_PAID_RESOURCES=yes FOLDER_ID=b1g... ./cloud/deploy.sh`
3. Измерения:
   ```bash
   export GATEWAY_URL=https://<домен шлюза>
   export SYNC_JWT_SECRET=$(yc lockbox payload get --name mp-sync-proto-jwt --key jwt)
   python -m cloud.measure --idle 1,5,15,30          # холодный и тёплый старт, восстановление 1000 записей
   python -m bench.bench --target url --url "$GATEWAY_URL" -n 30 --out cloud_benchmark.json
   python -m bench.cost_model bench/results/cloud_benchmark.json   # при наличии storage/variants
   ```
4. `./cloud/teardown.sh`

## Что измеряем и какие гипотезы проверяем

| Метрика | Источник | Гипотеза из локального этапа |
|---|---|---|
| cold start | `cloud.measure`: первый запрос после простоя, `requestsServedByInstance == 1` | старт процесса и драйвера < 0,5 с; основная часть — запуск экземпляра платформой (1–3 с) |
| warm start | 20 повторов | p50 push 8 / pull 10 — десятки мс (сеть + YDB) |
| первое обращение к YDB | `startup.first_query_ms` | 50–300 мс: discovery, сессия, TLS, IAM-токен |
| push / pull | `bench --target url` | latency пропорциональна числу записей так же, как локально |
| **RU push** | `X-YDB-RU` (заголовок Serverless) против `ruIoFormula` | ≈ 6 RU на мутацию + 3 RU на запрос. Локально записи не тарифицировались — это главная непроверенная цифра |
| RU pull | `X-YDB-RU` | 1 + 1 на запись (локально измерено) |
| память | `maxRssMb` | ~85 МБ, 512 МБ хватает с запасом |
| длительность обработки | `Server-Timing: app` | единицы мс на стороне приложения |
| восстановление 1000 записей после простоя | `restore_1000_ms` | холодный старт добавляется один раз, а не на каждую страницу |
