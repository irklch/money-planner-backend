"""Схема MVP (миграция 0004) на настоящем PostgreSQL: таблицы, ограничения, индексы, внешние ключи.

Каждый тест работает в транзакции, которая откатывается: данные других тестов не затрагиваются.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import init_engine

MVP_TABLES = {
    "devices",
    "users",
    "identities",
    "refresh_tokens",
    "apple_tokens",
    "apple_notifications",
    "sync_records",
    "sync_state",
    "sync_mutations",
    "import_deliveries",
    "import_ledger",
    "usage_counters",
    "rate_counters",
    "deletion_jobs",
    "deleted_accounts",
}
LEGACY_TABLES = {
    "legacy_users",
    "legacy_refresh_tokens",
    "categories",
    "expenses",
    "free_days",
    "expense_imports",
}


@pytest.fixture
async def db():
    async with init_engine().connect() as conn:
        tx = await conn.begin()
        yield conn
        await tx.rollback()


async def run(conn, sql: str, **params):
    return await conn.execute(text(sql), params)


async def violates(conn, sql: str, constraint: str, **params):
    """Оператор нарушает ровно указанное ограничение (savepoint — транзакция теста продолжается)."""
    sp = await conn.begin_nested()
    with pytest.raises(IntegrityError) as e:
        await conn.execute(text(sql), params)
    await sp.rollback()
    assert constraint in str(e.value.orig), str(e.value.orig)


def uid() -> str:
    return str(uuid.uuid4())


async def account(conn) -> str:
    user_id = uid()
    await run(conn, "INSERT INTO users (user_id) VALUES (:u)", u=user_id)
    return user_id


async def device(conn, account_id=None) -> str:
    device_id = uid()
    await run(
        conn,
        "INSERT INTO devices (device_id, platform, app_version, account_id) VALUES (:d, 'ios', '1.0', :a)",
        d=device_id,
        a=account_id,
    )
    return device_id


async def test_tables_and_revision(db):
    tables = set(
        (
            await run(db, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
    )
    assert MVP_TABLES <= tables
    assert LEGACY_TABLES <= tables
    assert tables == MVP_TABLES | LEGACY_TABLES | {"alembic_version"}
    assert (await run(db, "SELECT version_num FROM alembic_version")).scalar() == "0004_mvp_schema"


async def test_expiry_indexes_exist(db):
    # Таблицы со сроком хранения — с индексом по сроку (план E1): их чистит maintenance (E8).
    names = set((await run(db, "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")).scalars())
    assert {
        "ix_sync_mutations_created_at",
        "ix_import_deliveries_created_at",
        "ix_rate_counters_expires_at",
        "ix_refresh_tokens_expires_at",
        "ix_deleted_accounts_deleted_at",
        "ix_devices_last_seen_at",
        "ix_sync_records_tombstones",
        "uq_sync_records_user_version",
        "uq_deletion_jobs_active_user",
    } <= names


async def test_defaults(db):
    user_id = await account(db)
    row = (await run(db, "SELECT kind, state FROM users WHERE user_id = :u", u=user_id)).one()
    assert tuple(row) == ("account", "active")
    await run(db, "INSERT INTO sync_state (user_id) VALUES (:u)", u=user_id)
    assert tuple((await run(db, "SELECT last_version, tombstone_horizon FROM sync_state")).one()) == (0, 0)


async def test_devices_and_users_checks(db):
    await violates(
        db,
        "INSERT INTO devices (device_id, platform, app_version) VALUES (:d, 'windows', '1')",
        "ck_devices_platform",
        d=uid(),
    )
    await violates(
        db,
        "INSERT INTO devices (device_id, platform, app_version) VALUES (:d, 'ios', :v)",
        "ck_devices_app_version",
        d=uid(),
        v="x" * 33,
    )
    await violates(db, "INSERT INTO users (user_id, kind) VALUES (:u, 'guest')", "ck_users_kind", u=uid())
    await violates(db, "INSERT INTO users (user_id, state) VALUES (:u, 'gone')", "ck_users_state", u=uid())
    await violates(
        db,
        "INSERT INTO devices (device_id, platform, app_version, account_id) VALUES (:d, 'ios', '1', :a)",
        "fk_devices_account_id",
        d=uid(),
        a=uid(),
    )


async def test_refresh_tokens_constraints(db):
    d = await device(db)
    a = await account(db)
    ok = (
        "INSERT INTO refresh_tokens (token_hash, family_id, subject_kind, device_id, user_id, expires_at, "
        "revoked_at, revoked_reason) VALUES (:h, :f, :k, :d, :u, now() + interval '1 day', :ra, :rr)"
    )
    base = {"f": uid(), "d": d, "ra": None, "rr": None}
    await run(db, ok, h="a" * 64, k="guest", u=None, **base)
    await run(db, ok, h="b" * 64, k="account", u=a, **base)
    await violates(db, ok, "ck_refresh_tokens_token_hash", h="raw-token", k="guest", u=None, **base)
    await violates(db, ok, "ck_refresh_tokens_account_user", h="c" * 64, k="account", u=None, **base)
    await violates(db, ok, "ck_refresh_tokens_account_user", h="c" * 64, k="guest", u=a, **base)
    await violates(db, ok, "ck_refresh_tokens_subject_kind", h="c" * 64, k="admin", u=None, **base)
    await violates(
        db, ok, "ck_refresh_tokens_revoked_pair", h="c" * 64, k="guest", u=None, **{**base, "rr": "logout"}
    )
    await violates(
        db,
        ok,
        "ck_refresh_tokens_revoked_reason",
        h="c" * 64,
        k="guest",
        u=None,
        **{**base, "ra": datetime(2026, 10, 14, tzinfo=UTC), "rr": "stolen"},
    )
    await violates(
        db, ok, "fk_refresh_tokens_device_id", h="c" * 64, k="guest", u=None, **{**base, "d": uid()}
    )
    # Токен аккаунта может пережить строку users (account_deleted): ключа на users нет.
    await run(db, ok, h="d" * 64, k="account", u=uid(), **base)


async def test_sync_records_version_is_unique_per_user(db):
    u = await account(db)
    ins = (
        "INSERT INTO sync_records (user_id, entity_type, entity_id, version, created_at, updated_at, "
        "order_ts, device_id, schema_version, mutation_id, payload) "
        "VALUES (:u, :t, :e, :v, now(), now(), now(), :d, 1, :m, CAST(:p AS jsonb))"
    )
    common = {"u": u, "d": uid(), "p": '{"amountMinor": "100"}'}
    await run(db, ins, t="expense", e=uid(), v=1, m=uid(), **common)
    await run(db, ins, t="category", e=uid(), v=2, m=uid(), **common)
    await violates(db, ins, "uq_sync_records_user_version", t="expense", e=uid(), v=2, m=uid(), **common)
    await violates(db, ins, "ck_sync_records_entity_type", t="income", e=uid(), v=3, m=uid(), **common)
    await violates(db, ins, "ck_sync_records_version", t="expense", e=uid(), v=0, m=uid(), **common)
    await violates(
        db, ins, "fk_sync_records_user_id", t="expense", e=uid(), v=1, m=uid(), **{**common, "u": uid()}
    )
    # Та же версия у другого пользователя допустима.
    other = await account(db)
    await run(db, ins, t="expense", e=uid(), v=1, m=uid(), **{**common, "u": other})
    # Tombstone: payload может отсутствовать.
    await run(db, ins.replace("CAST(:p AS jsonb)", "NULL"), t="day_mark", e=uid(), v=3, m=uid(), **common)


async def test_sync_state_horizon_not_beyond_last_version(db):
    u = await account(db)
    await violates(
        db,
        "INSERT INTO sync_state (user_id, last_version, tombstone_horizon) VALUES (:u, 5, 6)",
        "ck_sync_state_horizon",
        u=u,
    )


async def test_one_active_deletion_job_per_account(db):
    u = uid()
    ins = (
        "INSERT INTO deletion_jobs (deletion_id, user_id, source, state, step, apple_revocation) "
        "VALUES (:id, :u, 'user', :s, 'start', 'pending')"
    )
    await run(db, ins, id=uid(), u=u, s="pending")
    await violates(db, ins, "uq_deletion_jobs_active_user", id=uid(), u=u, s="deleting")
    await violates(db, ins, "ck_deletion_jobs_state", id=uid(), u=uid(), s="cancelled")
    # Завершённые задания не мешают новому.
    await run(db, "UPDATE deletion_jobs SET state = 'done' WHERE user_id = :u", u=u)
    await run(db, ins, id=uid(), u=u, s="pending")


async def test_usage_counters_and_ledger(db):
    d = uid()
    ins = "INSERT INTO usage_counters (subject, feature, used) VALUES (:s, :f, :n)"
    await run(db, ins, s=f"device:{d}", f="imports", n=1)
    await violates(db, ins, "pk_usage_counters", s=f"device:{d}", f="imports", n=2)
    await violates(db, ins, "ck_usage_counters_subject", s=f"guest:{d}", f="imports", n=1)
    await violates(db, ins, "ck_usage_counters_feature", s=f"account:{uid()}", f="insights", n=1)
    await violates(db, ins, "ck_usage_counters_used", s=f"account:{uid()}", f="imports", n=-1)
    # Реестр импортов постоянный: переживает устройство и аккаунт (ключей нет).
    await run(
        db,
        "INSERT INTO import_ledger (import_id, device_id, account_id, confirmed_at) "
        "VALUES (:i, :d, :a, now())",
        i=uid(),
        d=uid(),
        a=uid(),
    )


async def test_import_deliveries(db):
    d = await device(db)
    ins = (
        "INSERT INTO import_deliveries (device_id, idem_key, import_id, file_mac) VALUES (:d, :k, :i, 'mac')"
    )
    key, import_id = uid(), uid()
    await run(db, ins, d=d, k=key, i=import_id)
    await violates(db, ins, "pk_import_deliveries", d=d, k=key, i=uid())
    await violates(db, ins, "uq_import_deliveries_import_id", d=d, k=uid(), i=import_id)
    await violates(db, ins, "fk_import_deliveries_device_id", d=uid(), k=uid(), i=uid())


async def test_apple_tokens_and_identities(db):
    u = await account(db)
    ins = "INSERT INTO apple_tokens (user_id, ciphertext, nonce, key_version) VALUES (:u, :c, :n, 1)"
    await run(db, ins, u=u, c=b"x" * 48, n=b"n" * 12)
    await violates(db, ins, "ck_apple_tokens_nonce", u=await account(db), c=b"x" * 48, n=b"n" * 16)
    await violates(db, ins, "fk_apple_tokens_user_id", u=uid(), c=b"x" * 48, n=b"n" * 12)
    ident = "INSERT INTO identities (provider, subject, user_id) VALUES (:p, :s, :u)"
    await run(db, ident, p="apple", s="001234.abc", u=u)
    await violates(db, ident, "pk_identities", p="apple", s="001234.abc", u=await account(db))
    await violates(db, ident, "ck_identities_provider", p="google", s="x", u=u)


async def test_deletion_order_from_plan_is_allowed_and_wrong_order_is_not(db):
    # План §2.5: sync_* → import_ledger и счётчики → apple_tokens → отвязка devices → users → identities.
    u = await account(db)
    d = await device(db, account_id=u)
    await run(db, "INSERT INTO sync_state (user_id) VALUES (:u)", u=u)
    await run(
        db,
        "INSERT INTO apple_tokens (user_id, ciphertext, nonce, key_version) VALUES (:u, :c, :n, 1)",
        u=u,
        c=b"x" * 48,
        n=b"n" * 12,
    )
    await run(db, "INSERT INTO identities (provider, subject, user_id) VALUES ('apple', 'sub-1', :u)", u=u)
    # Неверный порядок: users раньше зависимых строк.
    sp = await db.begin_nested()
    with pytest.raises(IntegrityError):
        await run(db, "DELETE FROM users WHERE user_id = :u", u=u)
    await sp.rollback()
    for sql in (
        "DELETE FROM sync_state WHERE user_id = :u",
        "DELETE FROM apple_tokens WHERE user_id = :u",
        "UPDATE devices SET account_id = NULL WHERE account_id = :u",
        "DELETE FROM users WHERE user_id = :u",
        "DELETE FROM identities WHERE user_id = :u",
    ):
        await run(db, sql, u=u)
    await run(db, "INSERT INTO deleted_accounts (user_id) VALUES (:u)", u=u)
    assert (await run(db, "SELECT account_id FROM devices WHERE device_id = :d", d=d)).scalar() is None
