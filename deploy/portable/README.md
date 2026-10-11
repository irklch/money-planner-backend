# Переносимое развёртывание

Один Linux-сервер с Docker у любого провайдера: PostgreSQL 16, API, ежедневная очистка guest, резервные копии и Caddy с Let's Encrypt. Сервисы облачного провайдера не нужны: реестр образов, Lockbox, Managed PG, Cloud Logging и metadata-IAM заменены на сборку на сервере, `.env`, PostgreSQL в контейнере, `json-file` логи и API-ключ ИИ.

Аварийный план (переезд, переключение DNS, возврат) — [`docs/disaster-recovery.md`](../../docs/disaster-recovery.md). Старое развёртывание в Yandex Cloud — [`deploy/README.md`](../README.md).

```
Интернет ──443──► caddy ──► api ──► db (том pgdata, наружу не публикуется)
                            maintenance ┘   ▲
                            backup ─────────┘── pg_dump → age → rclone ──► S3 другого провайдера
```

## Требования к серверу

| Параметр | Минимум | Комментарий |
|---|---|---|
| ОС | Ubuntu 22.04/24.04 или Debian 12 | x86_64 или arm64 |
| CPU / RAM | 1 vCPU / 2 ГБ | нагрузка MVP — доли запроса в секунду |
| Диск | 20 ГБ | образы ≈ 1,5 ГБ, БД — мегабайты |
| Сеть | публичный IPv4, входящие 22, 80, 443 | 80 нужен Let's Encrypt |
| Исходящий доступ | Docker Hub, PyPI, Debian, S3 копий, Let's Encrypt | без них не соберутся образы |

## Параметры и секреты

Все значения — в `deploy/portable/.env` на сервере (шаблон [`env.example`](env.example), права `600`). В git, metadata ВМ и чаты значения не попадают.

| Переменная | Секрет | Откуда | Что будет при потере |
|---|---|---|---|
| `API_DOMAIN` | нет | домен API, A-запись на сервер | — |
| `FENCE_EXPECTED_IP` | нет | публичный IPv4 сервера | — |
| `FENCE_MODE` | нет | `enforce`; `off` только до переключения DNS | — |
| `INSTANCE_NAME` | нет | имя сервера в путях копий | — |
| `POSTGRES_PASSWORD` | да | `openssl rand -base64 48` | задаётся заново при восстановлении |
| `JWT_SECRET` | да | `openssl rand -base64 48` | access-токены (15 мин) станут недействительны; клиенты обновят их по refresh |
| `TOKEN_DERIVATION_SECRET` | да | `openssl rand -base64 48`, отличный от JWT | refresh-токены продолжат работать (в БД только SHA-256); повтор `POST /auth/anonymous` в пределах 24 ч получит `409` |
| `AI_ENABLED`, `AI_FOLDER_ID`, `AI_API_KEY`, `AI_MODEL_*` | ключ — да | Yandex AI Studio, API-ключ сервисного аккаунта | `AI_ENABLED=false`: импорт работает, нераспознанные операции — «без категории» |
| `BACKUP_AGE_RECIPIENT` | нет (публичный ключ) | `age-keygen` на компьютере владельца | новые копии не снимаются |
| приватный ключ age | **да, критичный** | только у владельца, **не на сервере** | **все копии нельзя расшифровать** |
| `BACKUP_REMOTE`, `RCLONE_CONFIG_OFFSITE_*` | ключ доступа — да | бакет S3 у другого провайдера | копии остаются только на сервере |

Ни один секрет приложения не делает данные невосстановимыми: восстановлению нужен только приватный ключ age и доступ к бакету.

## Установка на чистый сервер

```bash
# 1. На компьютере владельца (один раз): ключ шифрования копий
age-keygen -o money-planner-backup.key      # приватный ключ → менеджер паролей + офлайн-копия
grep public money-planner-backup.key        # age1... → BACKUP_AGE_RECIPIENT

# 2. На сервере
git clone https://github.com/irklch/money-planner-backend.git /opt/money-planner
cd /opt/money-planner/deploy/portable
cp env.example .env && chmod 600 .env && nano .env
sudo ./install.sh
./smoke.sh                                   # healthz, запись и чтение БД
./smoke.sh "https://$API_DOMAIN"             # после выпуска сертификата
docker compose run --rm backup once          # первая копия и проверка доступа к бакету
docker compose run --rm backup list
```

Обновление: `git pull && sudo ./install.sh`. Миграции выполняет сервис `migrate` при каждом запуске.

## Резервные копии

- **Что копируется:** вся БД `money` (`pg_dump -Fc`). Других постоянных данных у сервера нет: выписки не сохраняются, сертификаты Caddy выпускаются заново.
- **Когда:** каждые `BACKUP_INTERVAL_HOURS` (6) и вручную `docker compose run --rm backup once`.
- **Где:** том `backups` на сервере (`BACKUP_KEEP_LOCAL_DAYS`, 3 дня) и `BACKUP_REMOTE/<INSTANCE_NAME>/` (`BACKUP_KEEP_REMOTE_DAYS`, 35 дней).
- **Защита:** файлы шифруются age на публичный ключ, рядом лежат `.sha256` и `.manifest.json` (ревизия миграций и число строк по таблицам, без данных). Сервер не может расшифровать копии, поэтому его взлом не раскрывает историю.
- **Хранилище:** S3 другого провайдера, не того, где работает сервер. Ключ — только к этому бакету; если провайдер позволяет — версионирование или блокировка удаления, чтобы взломанный сервер не стёр копии.
- **Контроль:** healthcheck сервиса `backup` становится `unhealthy`, если успешной копии нет дольше 2 интервалов (`docker compose ps`).
- **Только основной сервер:** перед каждой копией проверяется, что `API_DOMAIN` указывает на этот сервер (как `fence_check` у API). Иначе старая ВМ после переезда выложила бы «свежие» копии устаревшей БД.

### Восстановление

```bash
docker compose stop api maintenance
# приватный ключ — временно, на время восстановления, затем удалить
docker compose run --rm -v /root/age.key:/run/age.key:ro backup restore latest   # или имя money-YYYYmmddTHHMMSSZ
shred -u /root/age.key
docker compose up -d
./smoke.sh
```

`restore` проверяет `sha256`, отказывается писать в непустую БД (кроме `RESTORE_FORCE=yes`) и сверяет число строк с манифестом.

### Учения

`./drill.sh` — полный цикл в изолированном compose-проекте на синтетических данных: сервер → данные через API → зашифрованная копия → уничтожение сервера с томами → новый сервер → восстановление → проверка расходов и старой сессии. Запускается в CI (job `portable-deploy`) и вручную на любой машине с Docker.

## Защита от двух основных серверов

`app/jobs/fence_check.py` выполняется перед стартом `migrate`, `api` и `maintenance`. Если A-запись `API_DOMAIN` указывает не на `FENCE_EXPECTED_IP`, контейнер не запускается. Если DNS не отвечает, запуск продолжается. Сценарии — в [`docs/disaster-recovery.md`](../../docs/disaster-recovery.md#5-исключить-запись-в-две-бд).

## Логи

`docker compose logs -f api`. Драйвер `json-file`, до 5 файлов по 10 МБ на контейнер. Формат и белый список полей не изменились (`app/core/logging.py`).
