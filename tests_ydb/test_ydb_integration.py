"""Интеграция с локальной YDB: миграции, схема, транзакции, повторы, таймауты, недоступность."""

import asyncio
import dataclasses
import datetime as dt

import pytest
import ydb
import ydb.aio

from app.db_ydb.client import YdbClient, open_client
from app.db_ydb.errors import MigrationError, YdbUnavailable
from app.db_ydb.migrate import Migration, Migrator, load_migrations
from tests_ydb.conftest import YDB_DATABASE, ydb_settings
from tests_ydb.test_migrations_unit import EXPECTED_TABLES

pytestmark = pytest.mark.ydb


async def _describe(client: YdbClient, table: str):
    tc = ydb.aio.table.TableClient(client.driver)
    return await tc.describe_table(f"{YDB_DATABASE}/{client.table(table)}")


async def _tables(client: YdbClient) -> set[str]:
    scheme = ydb.aio.scheme.SchemeClient(client.driver)
    listing = await scheme.list_directory(f"{YDB_DATABASE}/{client.options.prefix}")
    return {c.name for c in listing.children if not c.is_directory()}


# --- миграции ---------------------------------------------------------------------------------
async def test_migrate_clean_database_then_rerun(fresh_client):
    m = Migrator(fresh_client)
    assert [x.version for x in await m.pending()] == [x.version for x in load_migrations()]
    applied = await m.migrate()
    assert [x.name for x in applied] == ["initial"]
    assert await _tables(fresh_client) == EXPECTED_TABLES | {"schema_migrations"}
    # Повторное применение: ничего не делает, схема та же.
    assert await Migrator(fresh_client).migrate() == []
    assert await Migrator(fresh_client).pending() == []
    status = await Migrator(fresh_client).status()
    assert all(rec is not None for _, rec in status)


async def test_interrupted_migration_is_completed_on_rerun(fresh_client):
    mig = load_migrations()[0]
    # «Прерванный» запуск: часть таблиц создана, запись о миграции не сделана.
    for stmt in mig.statements(fresh_client.options.prefix)[:5]:
        await fresh_client.ddl(stmt)
    assert [x.version for x in await Migrator(fresh_client).pending()] == [1]
    await Migrator(fresh_client).migrate()
    assert await _tables(fresh_client) == EXPECTED_TABLES | {"schema_migrations"}


async def test_concurrent_migrators(fresh_client):
    results = await asyncio.gather(Migrator(fresh_client).migrate(), Migrator(fresh_client).migrate())
    assert sum(len(r) for r in results) >= 1
    assert await Migrator(fresh_client).pending() == []


async def test_changed_applied_migration_is_rejected(fresh_client):
    await Migrator(fresh_client).migrate()
    original = load_migrations()
    changed = [dataclasses.replace(original[0], sql=original[0].sql + "\n-- edited\n")]
    with pytest.raises(MigrationError, match="changed after it was applied"):
        await Migrator(fresh_client, changed).migrate()
    with pytest.raises(MigrationError, match="file is missing"):
        await Migrator(fresh_client, []).status()


async def test_new_migration_is_applied_incrementally(fresh_client):
    await Migrator(fresh_client).migrate()
    extra = Migration(
        len(load_migrations()) + 1,
        "test_extra",
        "CREATE TABLE IF NOT EXISTS `{prefix}/extra_t` (id Utf8 NOT NULL, PRIMARY KEY (id));",
    )
    migs = [*load_migrations(), extra]
    assert [m.name for m in await Migrator(fresh_client, migs).migrate()] == ["test_extra"]
    assert "extra_t" in await _tables(fresh_client)


# --- схема -------------------------------------------------------------------------------------
async def test_schema_details(migrated):
    sync = await _describe(migrated, "sync_records")
    assert sync.primary_key == ["user_id", "entity_type", "entity_id"]
    idx = {i.name: i for i in sync.indexes}
    assert list(idx["by_version"].index_columns) == ["user_id", "version"]
    # COVER-колонки SDK в describe не возвращает (data_columns пуст); проверяются в CI через
    # `ydb scheme describe` (задача ydb в ci.yml).

    ttl = {
        name: (await _describe(migrated, name)).ttl_settings
        for name in ("sync_mutations", "import_deliveries", "rate_counters")
    }
    assert ttl["sync_mutations"].date_type_column.column_name == "created_at"
    assert ttl["sync_mutations"].date_type_column.expire_after_seconds == 30 * 86400
    assert ttl["import_deliveries"].date_type_column.expire_after_seconds == 7 * 86400
    assert ttl["rate_counters"].date_type_column.column_name == "expires_at"

    ident = await _describe(migrated, "identities")
    assert ident.primary_key == ["provider", "subject"]
    assert {i.name for i in ident.indexes} == {"by_user"}
    jobs = await _describe(migrated, "deletion_jobs")
    assert {c.name for c in jobs.columns} >= {"step", "cursor", "lease_until", "attempts", "apple_revocation"}


# --- запросы и транзакции ----------------------------------------------------------------------
async def test_typed_roundtrip(migrated):
    now = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.UTC)
    t = migrated.table("devices")
    await migrated.execute(
        f"""DECLARE $id AS Utf8; DECLARE $now AS Timestamp;
        UPSERT INTO `{t}` (device_id, platform, app_version, created_at, last_seen_at)
        VALUES ($id, "ios", "1.0.0", $now, $now);""",
        {"$id": ("dev-roundtrip", ydb.PrimitiveType.Utf8), "$now": (now, ydb.PrimitiveType.Timestamp)},
    )
    rows = (
        await migrated.execute(
            f"DECLARE $id AS Utf8; SELECT * FROM `{t}` WHERE device_id = $id;",
            {"$id": ("dev-roundtrip", ydb.PrimitiveType.Utf8)},
            idempotent=True,
        )
    )[0]
    assert rows[0]["platform"] == "ios" and rows[0]["account_id"] is None


def _increment(client: YdbClient, subject: str):
    t = client.table("usage_counters")
    key = {"$s": (subject, ydb.PrimitiveType.Utf8)}

    async def increment(tx):
        rows = (
            await tx.execute(
                f"DECLARE $s AS Utf8; SELECT used FROM `{t}` WHERE subject = $s AND feature = 'imports';", key
            )
        )[0]
        used = int(rows[0]["used"]) if rows else 0
        await asyncio.sleep(0.01)  # окно для конфликта
        await tx.execute(
            f"""DECLARE $s AS Utf8; DECLARE $used AS Uint64;
            UPSERT INTO `{t}` (subject, feature, used, updated_at)
            VALUES ($s, 'imports', $used, CurrentUtcTimestamp());""",
            {**key, "$used": (used + 1, ydb.PrimitiveType.Uint64)},
            commit=True,
        )
        return used + 1

    async def read() -> int:
        rows = (
            await client.execute(
                f"DECLARE $s AS Utf8; SELECT used FROM `{t}` WHERE subject = $s;", key, idempotent=True
            )
        )[0]
        return int(rows[0]["used"]) if rows else 0

    return increment, read


async def test_concurrent_transactions_are_serialized(prefix, migrated):
    """OCC: конкурентные инкременты одной строки конфликтуют (ABORTED) и повторяются без потерь."""
    client = await open_client(ydb_settings(prefix, ydb_retry_max_attempts=20, ydb_retry_budget_s=30))
    try:
        increment, read = _increment(client, "device:occ")
        n = 6
        await asyncio.gather(*(client.transaction(increment) for _ in range(n)))
        assert await read() == n
    finally:
        await client.close()


async def test_exhausted_retries_become_unavailable(prefix, migrated):
    """Конфликты сверх бюджета повторов → YdbUnavailable (503 + Retry-After), а не потерянные записи."""
    client = await open_client(ydb_settings(prefix, ydb_retry_max_attempts=1))
    try:
        increment, read = _increment(client, "device:occ-limit")
        results = await asyncio.gather(
            *(client.transaction(increment) for _ in range(6)), return_exceptions=True
        )
        failed = [r for r in results if isinstance(r, BaseException)]
        assert failed and all(isinstance(r, YdbUnavailable) for r in failed)
        assert await read() == len(results) - len(failed)  # каждое успешное увеличение учтено ровно раз
    finally:
        await client.close()


async def test_transaction_rolls_back_on_error(migrated):
    t = migrated.table("deleted_accounts")

    async def fail(tx):
        await tx.execute(
            f"UPSERT INTO `{t}` (user_id, deleted_at) VALUES ('rolled-back', CurrentUtcTimestamp());"
        )
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await migrated.transaction(fail)
    rows = (await migrated.execute(f"SELECT * FROM `{t}` WHERE user_id = 'rolled-back';", idempotent=True))[0]
    assert rows == []


async def test_query_error_is_not_retried_as_unavailable(migrated):
    with pytest.raises(ydb.issues.Error) as info:
        await migrated.execute(f"SELECT * FROM `{migrated.table('no_such_table')}`;")
    assert not isinstance(info.value, YdbUnavailable)


# --- таймауты и недоступность ------------------------------------------------------------------
async def test_query_timeout_becomes_unavailable(prefix):
    client = await open_client(
        ydb_settings(prefix, ydb_query_timeout_s=0.000001, ydb_retry_max_attempts=2, ydb_retry_budget_s=3)
    )
    try:
        with pytest.raises(YdbUnavailable):
            await client.execute("SELECT 1;", idempotent=True)
    finally:
        await client.close()


async def test_unreachable_endpoint_fails_fast(prefix):
    settings = ydb_settings(prefix, ydb_endpoint="grpc://localhost:1", ydb_connect_timeout_s=2)
    started = asyncio.get_running_loop().time()
    with pytest.raises(YdbUnavailable):
        await open_client(settings)
    assert asyncio.get_running_loop().time() - started < 10


async def test_ping(migrated):
    await migrated.ping()
