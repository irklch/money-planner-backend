# syntax=docker/dockerfile:1.7
# Минимальный образ sync-прототипа для Yandex Serverless Containers.
# Serverless Containers передаёт порт в $PORT; корневая ФС только для чтения.
# Этап зависимостей: ставим только рантайм-пакеты в отдельный префикс /install (без dev-инструментов).
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY pyproject.toml ./
RUN pip install --upgrade "pip>=25.1" && pip install --prefix=/install \
    "fastapi>=0.115,<1.0" "uvicorn>=0.30" "pydantic>=2.8,<3" "ydb[yc]>=3.33,<4" "pyjwt>=2.9"

# Итоговый образ: зависимости из /install и код syncproto (тесты, клиент и benchmark в образ не входят).
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv
COPY --from=deps /install /usr/local
COPY syncproto ./syncproto
# Запуск не от root.
RUN useradd --uid 10001 --no-create-home appuser
USER appuser
EXPOSE 8080
# Один worker: Serverless Containers масштабирует экземплярами, concurrency задаётся в ревизии.
CMD ["sh", "-c", "exec uvicorn syncproto.api:app --host 0.0.0.0 --port ${PORT} --no-access-log --timeout-keep-alive 30"]
