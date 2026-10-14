# Развёртывание

**Актуальное развёртывание — [`deploy/portable`](portable/README.md)**: любая Linux-ВМ, Docker Compose, PostgreSQL 16, Caddy, зашифрованные копии у другого провайдера (решение D24, `docs/production-migration-plan.md`). Аварийный план — [`docs/disaster-recovery.md`](../docs/disaster-recovery.md).

`deploy/Caddyfile` — конфигурация Caddy, которую монтирует `deploy/portable/docker-compose.yml`.

## Удалено в E1

Прежняя схема для Yandex Cloud (Container Optimized Image, `deploy/docker-compose.prod.yml`, Lockbox, Managed PostgreSQL, Container Registry, fluent-bit → Cloud Logging, metadata-IAM) удалена в E1 вместе с поддержкой Lockbox и metadata-IAM в коде. В облаке она не применялась (`docs/infra-audit-2026-10-11.md`). Текст инструкции — в истории git.
