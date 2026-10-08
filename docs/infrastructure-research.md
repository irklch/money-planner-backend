# Money Planner — исследование backend-инфраструктуры

Дата исследования и цен: **06.10.2026**. Все цены в ₽/мес с НДС 22%, если не указано иное.
Месяц = 720 ч (так считает Yandex Cloud в своих примерах).

## 1. Коротко

1. **Текущий план в `deploy/README.md` стоит не ~3 500 ₽, а ~9 600 ₽/мес.** VM 2 vCPU 100% / 4 ГБ с IP и диском — ~3 030 ₽. Рядом заложен Managed PostgreSQL `s2.micro` с 20 ГБ SSD — ещё ~6 560 ₽.
2. **Мощности с большим запасом.** Даже 10 000 MAU × 100 запросов — это ~0,4 запроса/с в среднем. Почти вся стоимость — фиксированные платежи.
3. **Serverless не даст 100–500 ₽, пока backend на PostgreSQL.** Вычисления на 100–1 000 MAU почти бесплатны, но минимальный Managed PG в Yandex (`b2.medium`) стоит ~3 350 ₽.
4. **Самая дорогая растущая статья — YandexGPT, а не хостинг.** `GET /insights` вызывает YandexGPT Pro на каждый запрос без кэша: ~28 000 ₽/мес при 10 000 MAU, ~85 000 ₽ в heavy-сценарии. Это одинаково для всех провайдеров.
5. **Рекомендация:** сейчас — одна маленькая VM в Yandex с PostgreSQL в Docker (~1 720 ₽); при росте — перенос БД в Managed PG и, при желании, приложения в Serverless Containers.

## 2. Что найдено в репозитории

| Что | Факт из кода | Как влияет на выбор |
|---|---|---|
| Стек | FastAPI, Python 3.12, async SQLAlchemy 2 + asyncpg, Alembic, PostgreSQL 16 | Нужен именно PostgreSQL |
| Зависимость от PostgreSQL | advisory-локи (`pg_advisory_xact_lock`, `pg_try_advisory_xact_lock`), UUID, ENUM, `octet_length` | YDB / Firebase = переписывание слоя данных |
| Развёртывание | VM с Container Optimized Image (2 vCPU / 4 ГБ), Caddy + Let's Encrypt, fluent-bit → Cloud Logging, Lockbox, Container Registry, Managed PG `s2.micro` | Текущий целевой план ≈ 9,6 тыс. ₽ |
| Выписка | Читается целиком в памяти, ФС контейнера read-only (`app/modules/imports/upload.py`) | Файл нигде не сохраняется; Object Storage для выписок не нужен; хранение = 0 ₽ |
| Импорт | Каждая операция — отдельная строка `expenses` (`source=import`, комментарий до 200 символов). Агрегации «день + категория» на backend нет | Объём данных всё равно мал; агрегация сэкономила бы ~30–50% строк |
| ИИ | Разбор выписки — YandexGPT Lite (по уникальным продавцам); каждый `GET /insights` — YandexGPT Pro | Переменная статья, считается отдельно |
| Rate limit | В памяти процесса, рассчитан на один инстанс (`app/core/rate_limit.py`) | В serverless лимиты будут действовать на каждый инстанс отдельно |
| Лимит загрузки | 10 МБ | Serverless Containers принимают до 3,5 МБ, API Gateway — до 2,5 МБ |
| Фоновые задачи | Сервис `maintenance` раз в сутки удаляет неактивных guest | В serverless нужен таймер-триггер |
| `refresh_tokens` | Старые записи не удаляются | ~1,2 ГБ/год при 10k MAU; стоит добавить очистку |

## 3. Допущения модели

- **Средний файл выписки — 50 КБ** (тестовый образец Альфы — 23,5 КБ; месячная выписка на 100–150 операций — 25–60 КБ). Входящий трафик бесплатный.
- Один импорт → ~100 строк расходов, плюс ~20 ручных расходов в месяц; ~300 байт на строку с индексами.
- Рост БД: 100 MAU — **0,04 ГБ/год**, 1 000 — **0,43 ГБ/год**, 10 000 — **4,3 ГБ/год** (+ `refresh_tokens` ~1,2 ГБ/год при 10k).
- Исходящий трафик: ~0,5 МБ на MAU в месяц → ~5 ГБ при 10k, ~15 ГБ в heavy. Меньше бесплатных 100 ГБ Yandex → 0 ₽.
- Логи: ~0,4 ГБ/мес при 10k; бесплатно 5 ГБ записи и 1 ГБ хранения → 0 ₽.
- Время обработки (для serverless): обычный запрос 0,1 с (минимальная единица тарификации), разбор выписки с ИИ 5 с, insights 3 с; холодные старты 40 / 10 / 2 с на MAU для 100 / 1k / 10k.
- **Базовый сценарий на MAU в месяц:** 100 API-запросов, 20 сессий, 1 импорт, 4 вызова insights.
- **Heavy:** 300 запросов, 3 импорта, 12 вызовов insights.
- Пиковая нагрузка при 10k MAU — порядка 5–10 запросов/с; одновременные загрузки всех пользователей не предполагаются.

## 4. Сравнительная таблица (₽/мес, без ИИ)

| Решение | Архитектура | 0 MAU | 100 | 1 000 | 10 000 | Heavy 10k | Масштабирование | Сложность миграции | Риски |
|---|---|---|---|---|---|---|---|---|---|
| **Y1. Yandex, текущий план** | VM 2×100%/4 ГБ + Managed PG s2.micro 20 ГБ SSD | 9 616 | 9 616 | 9 616 | 9 616 | 9 616 | Вручную; запас на 50k+ MAU | 0 | Переплата ~5× |
| **Y2. Yandex, одна VM с БД** | VM 2×20%/2 ГБ, PostgreSQL в Docker, pg_dump → Object Storage | 1 724 | 1 724 | 1 724 | 2 690¹ | 2 690 | Вручную, ~5 мин простоя | ~1 день | Бэкапы и обновления PG на вас; одна точка отказа |
| **Y3. Yandex VM + Managed PG** | VM 2×20%/2 ГБ + PG b2.medium 10 ГБ SSD | 4 842 | 4 842 | 4 842 | 5 805¹ | 5 805 | Вручную | ~0,5 дня (конфиг) | Минимальные |
| **Y4. Serverless Containers + API Gateway + Managed PG** | Контейнер без VM, PG b2.medium | 3 376 | 3 376 | 3 405 | 4 195 | 5 635 | Авто (compute) | 2–4 дня | Холодный старт 1–3 с; запрос ≤ 2,5 МБ; rate limit по инстансам |
| Y5. Serverless Containers + PG на маленькой VM | Контейнер + VM 2×20%/1 ГБ | 1 415 | 1 415 | 1 444 | 2 403 | 3 843 | Compute авто, БД вручную | 3–5 дней | Сложнее Y2 при экономии ~300 ₽ |
| Y6. Cloud Functions + Managed PG | Функция с ASGI-адаптером | 3 376 | 3 376 | 3 376 | 3 853 | 4 653 | Авто | 4–6 дней | Адаптация точки входа; лимит 3,5 МБ |
| Y7. Serverless Containers + YDB Serverless | Без PostgreSQL | ~24 | ~24 | ~80² | ~957² | ~2 644² | Полностью авто | **2–4 недели, переписывание** | Слой данных, advisory-локи, Alembic |
| **T1. Timeweb, одна VPS с БД** | Cloud-40 (2×3,3 ГГц, 2 ГБ, 40 ГБ NVMe) + PG в Docker | ~1 260 | ~1 260 | ~1 260 | ~1 460 | ~1 460 | Вручную | 1–2 дня | Нет Lockbox/IAM/Cloud Logging; ИИ по API-ключу |
| T2. Timeweb VPS + Managed PG | Cloud-40 + PG 1 vCPU/2 ГБ/20 ГБ | ~2 020 | ~2 020 | ~2 020 | ~2 220 | ~2 220 | Вручную | 1–2 дня | Те же |
| **A1. Amvera (PaaS)** | App Starter Plus + PG Starter, git push | 780 | 780 | 780 | 1 940 | ~3 390 | Ручная смена тарифа/инстансов | 1–2 дня | Небольшой провайдер; трафик и SLA описаны скупо |
| C1. Cloud.ru Evolution | Container Apps (scale-to-zero) + Managed PG 1 vCPU/2 ГБ | 1 775 | ~1 812 | ~2 722 | ~3 856 | ~4 891 | Авто | 2–3 дня | Оценка: окно до выключения экземпляра не задокументировано |
| S1. Selectel | VM Standard Line + DBaaS (мин. 2 vCPU/4 ГБ/32 ГБ) | ~5 473 | ~5 473 | ~5 473 | ~5 473 | ~5 480 | Вручную | 1–2 дня | Дорогой минимальный DBaaS |
| B1. Beget, одна VPS | 2 ГБ / 30 ГБ NVMe + IPv4 | ~960 | ~960 | ~960 | ~960+ | — | Вручную | 1–2 дня | Хостинг-уровень, всё на вас |
| Supabase / Firebase | Только для сравнения | Supabase Pro $25 ≈ 2 123 ₽ | | | | | | Переписывание (Firebase) | Оплата из РФ, данные вне РФ (152-ФЗ) — **не подходит** |

¹ При 10k MAU сервер увеличен: VM до 2×50%/4 ГБ; в Y3 диск PG до 20 ГБ.
² Оценка: ~5 Request Units на запрос; бесплатный объём YDB не учтён.

### Отдельная строка для всех вариантов — YandexGPT

| | 100 | 1 000 | 10 000 | Heavy 10k |
|---|---|---|---|---|
| Как в коде сейчас (insights без кэша, 4 вызова/MAU) | 282 | 2 820 | 28 200 | 84 600 |
| С кэшем insights (1 генерация на MAU в месяц) | 90 | 900 | 9 000 | 14 200 |

- Разбор выписки: ~1 300 токенов × 0,2 ₽/1000 (YandexGPT Lite) ≈ **0,26 ₽** за импорт.
- Insights: ~800 токенов × 0,8 ₽/1000 (YandexGPT Pro 5.1) ≈ **0,64 ₽** за вызов. Если `yandexgpt/latest` указывает на Pro 5 (1,2 ₽/1000) — ~0,96 ₽.

### Структура расходов

| Тип | Что входит |
|---|---|
| Фиксированные | VM, публичный IP, Managed PG (compute), Lockbox, Container Registry |
| Зависят от пользователей | Размер VM / тариф PaaS при росте (ступенчато) |
| Зависят от запросов | Serverless CPU/RAM/вызовы, API Gateway, YandexGPT |
| Зависят от объёма данных | Диск БД, бэкапы сверх бесплатного объёма |

## 5. Расчёты по вариантам

### Использованные цены Yandex Cloud (с НДС, «действует с 1 мая 2026»)

| Позиция | Цена |
|---|---|
| vCPU Ice Lake 20% / 50% / 100% | 0,52 / 0,75 / 1,24 ₽/ч |
| RAM VM | 0,33 ₽/ГБ·ч |
| Диск SSD / HDD | 0,0199 / 0,0048 ₽/ГБ·ч |
| Публичный IP | 0,26352 ₽/ч = **190 ₽/мес** |
| Managed PG, Cascade Lake 100% / 50% vCPU | 2,08 / 1,0892 ₽/ч |
| Managed PG, RAM | 0,5649 ₽/ГБ·ч |
| Managed PG, SSD | 0,0218 ₽/ГБ·ч; бэкапы бесплатны в пределах размера хранилища |
| Serverless Containers | 5,69 ₽/vCPU·ч, 3,79 ₽/ГБ·ч, 18,97 ₽ за 1 млн вызовов; бесплатно 5 vCPU·ч, 10 ГБ·ч, 1 млн вызовов |
| Cloud Functions | 6,48 ₽/ГБ·ч, 18,97 ₽ за 1 млн вызовов; бесплатно 10 ГБ·ч, 1 млн вызовов |
| API Gateway | 142,3 ₽ за 1 млн; первые 100 тыс. бесплатно |
| Lockbox | 0,0274 ₽/ч за версию секрета = **20 ₽** |
| Container Registry | 0,004575 ₽/ГБ·ч; ~1,25 ГБ образов ≈ **4 ₽** |
| Object Storage | 0,0033 ₽/ГБ·ч; 1 ГБ бесплатно |
| Исходящий трафик | 100 ГБ бесплатно, далее 1,42 ₽/ГБ |
| Cloud Logging | 5 ГБ записи и 1 ГБ хранения бесплатно |

### Y1 — текущий план

VM 720×(2×1,24 + 4×0,33) = 2 736
+ загрузочный диск 30 ГБ HDD = 104 (размер диска COI — допущение)
+ IP 190
+ PG s2.micro 720×(2×2,08 + 8×0,5649) = 6 249
+ PG SSD 20 ГБ = 314
+ Lockbox 20 + Registry 4
= **9 616 ₽**, полностью фиксированно.

### Y2 — одна VM с БД

VM 720×(2×0,52 + 2×0,33) = 1 224 + SSD 20 ГБ 287 + IP 190 + Lockbox/Registry 24 = **1 724 ₽**.
Бэкапы: pg_dump в Object Storage, в пределах 1 ГБ бесплатно → 0 ₽.

10k MAU: VM 2×50%/4 ГБ 2 030 + SSD 30 ГБ 430 + IP 190 + 24 + бэкапы (7 копий × 1 ГБ) 17 = **2 690 ₽**.

### Y3 — VM + Managed PG

VM 1 224 + загрузочный HDD 15 ГБ 52 + IP 190 + PG b2.medium 720×(2×1,0892 + 4×0,5649) = 3 195 + PG SSD 10 ГБ 157 + 24 = **4 842 ₽**.
10k: VM 2 030, PG SSD 20 ГБ 314 → **5 805 ₽**.

### Y4 — Serverless Containers + API Gateway + Managed PG

Фиксированная часть: PG b2.medium 3 195 + SSD 157 + 24 = **3 376 ₽**.
Переменная часть (контейнер 1 vCPU / 0,5 ГБ):

| | Машинное время | CPU | RAM | Вызовы | API Gateway | Итого |
|---|---|---|---|---|---|---|
| 100 MAU | 1,8 ч | 0 (free tier) | 0 | 0 | 0 | **3 376** |
| 1 000 MAU | 10,1 ч | (10,1−5)×5,69 = 29 | 0 | 0 | 0 | **3 405** |
| 10 000 MAU | 79 ч | 422 | 112 | 0 (1 млн бесплатно) | 900 тыс. × 142,3 = 128 | + PG SSD 20 ГБ (+157) → **4 195** |
| Heavy 10k | 226 ч | 1 260 | 391 | 38 | 413 | + 157 → **5 635** |

Подготовленный (всегда тёплый) экземпляр 1 vCPU / 0,5 ГБ стоил бы ~1 170 ₽/мес — это съедает экономию, поэтому холодные старты принимаются.

### Y5 — Serverless + PG на маленькой VM

VM 2×20%/1 ГБ 986 + SSD 15 ГБ 215 + IP 190 + 24 = 1 415 ₽ + serverless-часть как в Y4.
IP нужен VM для обновлений; альтернатива — NAT-шлюз (0,39528 ₽/ч ≈ 285 ₽).

### Y6 — Cloud Functions (512 МБ)

10k: (39,6 − 10) ГБ·ч × 6,48 = 192 + API Gateway 128 + PG 3 533 → **3 853 ₽**.
Heavy: 669 + 38 + 413 + PG → **4 653 ₽**.

### Y7 — YDB Serverless

24,64 ₽ за 1 млн RU, 0,0342 ₽/ГБ·ч хранения. 10k: 5 млн RU ≈ 123 + 6 ГБ ≈ 148 + контейнеры 662 + 24 ≈ **957 ₽**. Требует переписывания слоя данных.

### T1 — Timeweb, одна VPS

Cloud-40: 900 ₽ при оплате за 12 мес (скидка 10%) → ≈ 1 000 ₽ помесячно
+ публичный IP 200 ₽ (в калькуляторе указан отдельно — учтён консервативно)
+ автобэкапы 6 ₽/ГБ × ~10 ГБ = 60
= **~1 260 ₽**. 10k: Cloud-50 (4 ГБ) ≈ 1 200 → **~1 460 ₽**. Исходящий трафик не тарифицируется.

### T2 — Timeweb VPS + Managed PG

VPS 1 000 + IP 200 + Managed PG 790 (1×3,3 ГГц / 2 ГБ / 20 ГБ NVMe) + бэкапы ~30 = **~2 020 ₽**. 10k: Cloud-50 → **~2 220 ₽**.

### A1 — Amvera

Приложение Starter Plus (0,5 CPU / 1 ГБ) 490 + PG Starter 290 за реплику (бэкапы бесплатно) = **780 ₽**.
10k: Standard 1 450 + PG Starter Plus 490 = **1 940 ₽**. Heavy: 2 × Standard + PG Starter Plus = **~3 390 ₽**.

### C1 — Cloud.ru Evolution

PG: 720×(1,49145 + 2×0,4026) + 10 ГБ × 0,016836 × 720 = **1 775 ₽** (совпадает со страницей продукта).
Container Apps: 1,891 ₽/vCPU·ч, 1,257 ₽/ГБ·ч; бесплатно 25 vCPU·ч и 50 ГБ·ч. Оплачивается время жизни экземпляра, а не запросы.
Контейнер 0,5 vCPU / 1 ГБ активен по оценке ~67 / 480 / 940 / 1 410 ч → 37 / 947 / 1 960 / 2 995 ₽. При 10k и heavy диск PG 20 ГБ (+121) → **3 856 / 4 891 ₽**.

### S1 — Selectel

DBaaS минимум: 2 × 1 024,63 + 4 × 342,10 + 32 × 22,11 = **4 125 ₽**.
VM «от 948,50» + IP 189,57 + диск 20 ГБ × 10,48 = 1 348 ₽. Итого **~5 473 ₽**. Трафик свыше 10 ГБ — 1,13 ₽/ГБ.

### B1 — Beget

27 ₽/день + IPv4 5 ₽/день = 32 ₽/день ≈ **960 ₽/мес**.

### Иностранные решения

Supabase Pro — от $25/мес ≈ 2 123 ₽ (курс ЦБ 84,9309 на 06.10.2026); free-план засыпает через неделю неактивности. FastAPI всё равно нужно где-то хостить. Оплата российскими картами недоступна, данные вне РФ — **не подходит**. Firebase — то же плюс полное переписывание.

## 6. Масштабирование

| Вариант | 100 → 1 000 | 1 000 → 10 000 | Autoscaling | Первое узкое место |
|---|---|---|---|---|
| Y2, T1, B1 (одна VM с БД) | Ничего не меняется | Поднять до 2×50% / 4 ГБ вручную (~5 мин простоя) | Нет | RAM общего сервера (PG + приложение), бэкапы; после ~20–30k MAU — вынести БД |
| Y3 (VM + Managed PG) | Ничего | Увеличить VM | Нет | Ничего до 10k+; дальше — класс PG |
| Y4 (Serverless + Managed PG) | Ничего | Compute сам; диск PG 20 ГБ | Да (compute) | Соединения с БД: пул на инстанс нужно снизить до 1–2 (пулер на 6432 уже используется) |
| Amvera | Ничего | Сменить тариф / добавить инстансы | Нет (в разработке у провайдера) | Тариф приложения |
| Cloud.ru | Ничего | Сам | Да, scale-to-zero | Стоимость «тёплого» времени экземпляра |

Переход от БД на VM к Managed PG — pg_dump/restore, около полудня; архитектура приложения не меняется.

## 7. Безопасность и 152-ФЗ

- **TLS.** VM — Caddy + Let's Encrypt (уже есть). Serverless — API Gateway + Certificate Manager (бесплатно). Amvera — бесплатный SSL заявлен.
- **Изоляция БД.** Managed PG — только приватная подсеть, SSL уже обязателен в коде. PostgreSQL на VM — порт не публиковать, только внутренняя сеть Docker.
- **Секреты.** Yandex — Lockbox (уже есть). Timeweb, Amvera, Beget — переменные окружения.
- **Бэкапы.** Managed PG — автоматически, 7 дней, PITR. На VM — обязательно ежедневный pg_dump в Object Storage плюс регулярная проверка восстановления.
- **Выписки.** Не сохраняются: разбор в памяти, ФС read-only.
- **Логи.** Приложение пишет только белый список полей, access-лог Caddy выключен. В serverless проверить, что логи API Gateway не содержат query string.
- **ИИ.** В YandexGPT уходят только маскированные названия операций, заголовок `x-data-logging-enabled: false`.
- **152-ФЗ.**
  - Данные граждан РФ должны записываться и храниться в базах на территории РФ (ч. 5 ст. 18): выбирать регионы РФ (у Timeweb — не Нидерланды, у Amvera — Москва, не Варшава).
  - Уведомить Роскомнадзор как оператор ПДн, иметь политику конфиденциальности и согласие.
  - Анонимный ID вместе с расходами — скорее всего тоже ПДн; после Sign in with Apple — точно.
  - Это не юридическое заключение.

## 8. Рекомендации

| | Вариант | Стоимость |
|---|---|---|
| **A. Разработка / MVP** | Y2 — одна VM Yandex 2×20% / 2 ГБ, PostgreSQL в Docker | ~1 720 ₽ |
| **B. ~1 000 MAU** | Y2; если нужна managed-БД — Y4 | ~1 720 / ~3 400 ₽ |
| **C. ~10 000 MAU** | Y4 — Serverless Containers + API Gateway + Managed PG (или Y3 без смены способа деплоя) | ~4 200 (heavy ~5 600) / ~5 800 ₽ |
| **D. Самый дешёвый приемлемый** | Amvera | 780 ₽ |
| **E. Проще всего в обслуживании** | Y4; из PaaS — Amvera | ~3 400 ₽ / 780 ₽ |
| **F. Для Money Planner** | Y2 сейчас → Managed PG при ~3–5k MAU или раньше, если важна надёжность | ~1 720 ₽ |

### Почему F

1. Код почти не меняется: остаются Lockbox, ИИ через IAM, Cloud Logging, Container Registry и CI. Нет лимита 2,5 МБ, холодных стартов и проблемы rate limit по инстансам.
2. Экономия в **5,6 раза** против текущего плана (9,6k → 1,7k) и в 2 раза против «VM за 3,5k».
3. Путь роста без переписывания: pg_dump → Managed PG, затем при желании Serverless Containers.
4. Переезд к Timeweb или Amvera экономит ещё 500–900 ₽, но ломает интеграцию с Yandex, а ИИ всё равно остаётся в Yandex.

### Главный вопрос: есть ли смысл держать VM за ~3 500 ₽?

Нет. VM 2×100% / 4 ГБ в десятки раз превышает реальную нагрузку, а вместе с запланированным Managed PG `s2.micro` счёт составит ~9 600 ₽. Без серьёзного усложнения та же схема на VM 2×20% / 2 ГБ с PostgreSQL рядом стоит ~1 700 ₽. «Serverless за 100–500 ₽» при PostgreSQL недостижим (Managed PG — минимум ~3,35k); возможен только с YDB, а это переписывание слоя данных, сейчас не оправданное.

**Отдельно:** кэшировать `/insights` по хэшу фактов. При 10k MAU это ~19k ₽/мес экономии — больше всей инфраструктуры.

## 9. План перехода на Y2

1. **Текущая архитектура:** VM с COI (2 vCPU / 4 ГБ) + Managed PG `s2.micro` + Caddy + fluent-bit + Lockbox + Container Registry.
2. **Предлагаемая:** VM с COI (2×20% / 2 ГБ, SSD 20 ГБ) + `postgres:16` в compose + ежедневный pg_dump в Object Storage + расписание снимков диска.
3. **Меняется:** размер VM, источник БД, способ бэкапов.
4. **Остаётся:** весь код приложения, Lockbox, IAM для ИИ, Caddy, Cloud Logging, CI.
5. **Правки:**
   - `deploy/docker-compose.prod.yml`: сервис `postgres` с томом и сервис бэкапа;
   - `DATABASE_URL` в Lockbox — локальный хост;
   - убрать `DB_SSL_ROOT_CERT` (подключение внутри Docker-сети);
   - обновить `deploy/README.md`;
   - код Python не меняется.
6. **Объём:** ~1 день, включая проверку восстановления.
7. **Поддержка:** ответственность за бэкапы и обновления PostgreSQL, ~1 час в месяц.

### Что понадобится для Y4 позже (~2–4 дня)

- CMD с портом из `$PORT`;
- пул соединений 1–2;
- лимит загрузки ≤ 2 МБ (и изменение контракта `file_too_large`);
- спецификация API Gateway вместо Caddy, домен и сертификат;
- очистка неактивных пользователей через таймер-триггер вместо сервиса `maintenance` (защищённый HTTP-эндпоинт);
- `alembic upgrade head` из CI;
- осознанно принять rate limit на каждый инстанс (или перенести его в PostgreSQL).

## 10. Источники

Цены сняты 06.10.2026.

- **Yandex Cloud:**
  [Compute](https://yandex.cloud/ru/docs/compute/pricing) ·
  [VPC](https://yandex.cloud/ru/docs/vpc/pricing) ·
  [Managed PostgreSQL](https://yandex.cloud/ru/docs/managed-postgresql/pricing) ·
  [классы хостов PG](https://yandex.cloud/ru/docs/managed-postgresql/concepts/instance-types) ·
  [Serverless Containers](https://yandex.cloud/ru/docs/serverless-containers/pricing) ·
  [лимиты Serverless Containers](https://yandex.cloud/ru/docs/serverless-containers/concepts/limits) ·
  [Cloud Functions](https://yandex.cloud/ru/docs/functions/pricing) ·
  [API Gateway](https://yandex.cloud/ru/docs/api-gateway/pricing) ·
  [лимиты API Gateway](https://yandex.cloud/ru/docs/api-gateway/concepts/limits) ·
  [Lockbox](https://yandex.cloud/ru/docs/lockbox/pricing) ·
  [Container Registry](https://yandex.cloud/ru/docs/container-registry/pricing) ·
  [Cloud Logging](https://yandex.cloud/ru/docs/logging/pricing) ·
  [Monitoring](https://yandex.cloud/ru/docs/monitoring/pricing) ·
  [Object Storage](https://yandex.cloud/ru/docs/storage/pricing) ·
  [DNS](https://yandex.cloud/ru/docs/dns/pricing) ·
  [Certificate Manager](https://yandex.cloud/ru/docs/certificate-manager/pricing) ·
  [YDB Serverless](https://yandex.cloud/ru/docs/ydb/pricing/serverless) ·
  [Free tier](https://yandex.cloud/ru/docs/billing/concepts/serverless-free-tier) ·
  [AI Studio](https://aistudio.yandex.ru/ru/docs/ai-studio/pricing)
- **Timeweb Cloud:** [VPS](https://timeweb.cloud/services/vds-vps) · [PostgreSQL](https://timeweb.cloud/services/postgresql) · [калькулятор](https://timeweb.cloud/prices). Цену App Platform для backend на официальных страницах найти не удалось — не считал.
- **Cloud.ru:**
  [тариф Container Apps (PDF, версия от 18.09.2026)](https://cdn.cloud.ru/docs/legal/tariffs/evolution/current-version/container-apps.pdf) ·
  [описание услуги](https://cdn.cloud.ru/docs/legal/contracts/terms-of-service/evolution/current-version/description-container-apps.pdf) ·
  [тариф Managed PostgreSQL (PDF)](https://cdn.cloud.ru/docs/legal/tariffs/evolution/current-version/managed-postgresql.pdf) ·
  [страница Managed PostgreSQL](https://cloud.ru/products/evolution-managed-postgresql) ·
  [free tier](https://cloud.ru/offers/free-tier) (указано «исключительно для тестирования»; в FAQ старые цены Container Apps 4,32 ₽ — использованы цены из PDF-тарифа)
- **Amvera:** [тарифы](https://docs.amvera.ru/general/price.html) · [PostgreSQL](https://docs.amvera.ru/databases/postgreSQL.html)
- **Selectel:** [прайс](https://selectel.ru/prices/) · [облачные серверы](https://selectel.ru/services/cloud/servers/) · [конфигурации PG](https://docs.selectel.ru/managed-databases/postgresql/configurations/)
- **Beget:** [VPS](https://beget.com/ru/vps)
- **Supabase:** [pricing](https://supabase.com/pricing). Курс USD — [ЦБ РФ на 06.10.2026](https://www.cbr.ru/scripts/XML_daily.asp?date_req=06/10/2026)

VK Cloud подробно не считался: по предварительной проверке он не дешевле Yandex для такой нагрузки, цены не подтверждены.
