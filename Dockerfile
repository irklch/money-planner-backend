# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
RUN pip install --upgrade "pip>=25.1"

FROM base AS deps
COPY pyproject.toml ./
COPY app/__init__.py app/__init__.py
RUN pip install .

FROM deps AS test
RUN pip install --group dev
COPY . .
CMD ["pytest", "-q"]

FROM base AS runtime
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
RUN useradd --uid 10001 --no-create-home appuser
USER appuser
EXPOSE 8000
# Файлы выписок не пишутся на диск: корневая ФС контейнера в проде read-only (см. deploy/).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]
