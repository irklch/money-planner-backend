"""Схема YDB прототипа. Запуск: `python -m syncproto.schema create|drop|describe`.

Отличия от architecture-local-first.md §8.1:
- индекс `by_version` явно GLOBAL SYNC: ASYNC-индекс eventual и ломает pull по курсору;
- `COVER` — pull читает только индекс, без lookup в основную таблицу (вариант `cover`);
- в `sync_records` добавлены `order_ts` (вариант B) и `mutation_id` (диагностика);
- `sync_state.last_version` — единственная «горячая» строка пользователя, через неё
  сериализуются конкурентные push одного пользователя (OCC YDB).
"""

from __future__ import annotations

import argparse

# argparse — CLI; asyncio — запуск; ydb — выполнение DDL.
import asyncio

import ydb

# Колонки записи, которые читает приложение (кроме user_id).
RECORD_COLUMNS = (
    "entity_type",
    "entity_id",
    "version",
    "created_at",
    "updated_at",
    "deleted_at",
    "order_ts",
    "hlc",
    "device_id",
    "schema_version",
    "mutation_id",
    "payload",
    "server_updated_at",
)
# Колонки, которые хранятся прямо в индексе by_version (COVER): pull читает только индекс.
COVER_COLUMNS = tuple(c for c in RECORD_COLUMNS if c not in ("entity_type", "entity_id", "version"))


# DDL трёх таблиц. cover=False — вариант индекса без копии данных (дешевле хранение, дороже pull).
# mutations_ttl — сколько хранится журнал мутаций (ISO 8601: P30D = 30 дней).
def ddl(prefix: str, cover: bool = True, mutations_ttl: str = "P30D") -> list[str]:
    cover_clause = f" COVER ({', '.join(COVER_COLUMNS)})" if cover else ""
    return [
        # sync_records — текущее состояние каждой записи пользователя (включая tombstones).
        # PK (user_id, entity_type, entity_id) — точечный доступ к записи; все данные пользователя рядом.
        # by_version — синхронный глобальный индекс (user_id, version): pull идёт диапазоном по нему.
        # Автопартиционирование по размеру и нагрузке — масштабирование без ручного шардирования.
        f"""
        CREATE TABLE IF NOT EXISTS `{prefix}/sync_records` (
            user_id Utf8 NOT NULL,
            entity_type Utf8 NOT NULL,
            entity_id Utf8 NOT NULL,
            version Uint64 NOT NULL,
            created_at Timestamp NOT NULL,
            updated_at Timestamp NOT NULL,
            deleted_at Timestamp,
            order_ts Timestamp NOT NULL,
            hlc Utf8,
            device_id Utf8 NOT NULL,
            schema_version Uint32 NOT NULL,
            mutation_id Utf8 NOT NULL,
            payload Json,
            server_updated_at Timestamp NOT NULL,
            PRIMARY KEY (user_id, entity_type, entity_id),
            INDEX by_version GLOBAL SYNC ON (user_id, version){cover_clause}
        ) WITH (
            AUTO_PARTITIONING_BY_SIZE = ENABLED,
            AUTO_PARTITIONING_BY_LOAD = ENABLED
        )
        """,
        # sync_state — одна строка на пользователя: последняя выданная версия и горизонт очистки tombstones.
        # Через эту строку сериализуются конкурентные push одного пользователя (OCC).
        f"""
        CREATE TABLE IF NOT EXISTS `{prefix}/sync_state` (
            user_id Utf8 NOT NULL,
            last_version Uint64 NOT NULL,
            tombstone_horizon Uint64 NOT NULL,
            updated_at Timestamp NOT NULL,
            PRIMARY KEY (user_id)
        ) WITH (AUTO_PARTITIONING_BY_LOAD = ENABLED)
        """,
        # sync_mutations — журнал обработанных мутаций для идемпотентных повторов. Удаляется по TTL.
        f"""
        CREATE TABLE IF NOT EXISTS `{prefix}/sync_mutations` (
            user_id Utf8 NOT NULL,
            mutation_id Utf8 NOT NULL,
            request_hash Utf8 NOT NULL,
            result Json NOT NULL,
            created_at Timestamp NOT NULL,
            PRIMARY KEY (user_id, mutation_id)
        ) WITH (
            AUTO_PARTITIONING_BY_LOAD = ENABLED,
            TTL = Interval("{mutations_ttl}") ON created_at
        )
        """,
    ]


# DDL удаления таблиц (тесты, пересоздание).
def drop_ddl(prefix: str) -> list[str]:
    return [f"DROP TABLE IF EXISTS `{prefix}/{t}`" for t in ("sync_records", "sync_state", "sync_mutations")]


# Выполнить DDL по очереди.
async def apply(pool: ydb.aio.QuerySessionPool, statements: list[str]) -> None:
    for s in statements:
        await pool.execute_with_retries(s)


# CLI: python -m syncproto.schema create|drop|recreate [--prefix ...] [--no-cover].
async def _main() -> None:
    from .config import Settings
    from .store_ydb import open_driver

    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "drop", "recreate"])
    p.add_argument("--prefix")
    p.add_argument("--no-cover", action="store_true")
    a = p.parse_args()
    # Подключение — по тем же переменным окружения, что у приложения.
    s = Settings.from_env(require_secret=False)  # схеме JWT не нужен
    prefix = a.prefix or s.ydb_table_prefix
    driver = await open_driver(s)
    async with ydb.aio.QuerySessionPool(driver) as pool:
        if a.action in ("drop", "recreate"):
            await apply(pool, drop_ddl(prefix))
        if a.action in ("create", "recreate"):
            await apply(pool, ddl(prefix, cover=not a.no_cover))
    await driver.stop()
    print(f"{a.action}: {prefix} ok")


if __name__ == "__main__":
    asyncio.run(_main())
