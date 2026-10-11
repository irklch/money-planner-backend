# Контракт API v1

Источник истины для backend, iOS и Android. Правила (версионирование, совместимость, ошибки, идемпотентность, повторы, лимиты, пагинация) — [`docs/api-contract.md`](../docs/api-contract.md).

| Файл | Назначение |
|---|---|
| `openapi.yaml` | публичный контракт OpenAPI 3.1 (публикуется в API Gateway) |
| `openapi-internal.yaml` | `/internal/*`, только для таймер-триггеров, в шлюзе не публикуется |
| `system-categories.json` | реестр `SYSTEM_CATEGORY_IDS` |
| `config-parameters.json` | параметры конфигурации, константы протокола, повторы клиента |
| `vectors/` | тест-векторы для iOS, Android и сервера |
| `tests/` | контрактные тесты (без БД) |

```bash
python scripts/contract_spec.py --check
pytest contract/tests
```
