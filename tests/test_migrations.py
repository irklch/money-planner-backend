"""Миграции E1 на отдельных временных БД: чистая установка, переход стенда с 0003, downgrade."""

import asyncio
import os
import subprocess
import sys
import uuid

import asyncpg
import pytest

from tests.conftest import _plain

HEAD = "0004_mvp_schema"
PREVIOUS = "0003_expense_imports_result"


@pytest.fixture
async def scratch_db():
    """Пустая БД и функция alembic(*args) для неё."""
    url = os.environ["DATABASE_URL"]
    admin = os.environ.get("ADMIN_DATABASE_URL", url.rsplit("/", 1)[0] + "/postgres")
    name = f"mig_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(_plain(admin))
    await conn.execute(f'CREATE DATABASE "{name}"')
    db_url = url.rsplit("/", 1)[0] + f"/{name}"

    async def alembic(*args: str) -> None:
        env = {**os.environ, "DATABASE_URL": db_url}
        cmd = [sys.executable, "-m", "alembic", *args]
        result = await asyncio.to_thread(subprocess.run, cmd, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[-2000:]

    db = await asyncpg.connect(_plain(db_url))
    yield db, alembic
    await db.close()
    await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await conn.close()


async def tables(db) -> set[str]:
    rows = await db.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    return {r["table_name"] for r in rows}


async def revision(db) -> str:
    return await db.fetchval("SELECT version_num FROM alembic_version")


async def test_clean_install_reaches_head(scratch_db):
    db, alembic = scratch_db
    await alembic("upgrade", "head")
    assert await revision(db) == HEAD
    assert {"devices", "users", "refresh_tokens", "sync_records", "legacy_users"} <= await tables(db)
    await alembic("check")  # модели и схема совпадают


async def test_test_stand_path_from_0003_keeps_legacy_data(scratch_db):
    """Существующий стенд: схема 0003 с данными → upgrade head без сброса БД."""
    db, alembic = scratch_db
    await alembic("upgrade", PREVIOUS)
    user_id, family = uuid.uuid4(), uuid.uuid4()
    await db.execute("INSERT INTO users (id, is_anonymous) VALUES ($1, true)", user_id)
    await db.execute(
        "INSERT INTO refresh_tokens (id, user_id, token_hash, family_id, expires_at) "
        "VALUES ($1, $2, 'legacy-hash', $3, now() + interval '1 day')",
        uuid.uuid4(),
        user_id,
        family,
    )
    await db.execute(
        "INSERT INTO categories (id, user_id, name, emoji, color_index) VALUES ($1, $2, 'Мои', '🧪', 1)",
        uuid.uuid4(),
        user_id,
    )

    await alembic("upgrade", "head")
    assert await revision(db) == HEAD
    assert await db.fetchval("SELECT count(*) FROM legacy_users WHERE id = $1", user_id) == 1
    assert await db.fetchval("SELECT token_hash FROM legacy_refresh_tokens WHERE user_id = $1", user_id) == (
        "legacy-hash"
    )
    # Новые таблицы MVP пустые: данные старой схемы в них не переносятся (D16).
    assert await db.fetchval("SELECT count(*) FROM users") == 0
    assert await db.fetchval("SELECT count(*) FROM refresh_tokens") == 0
    # Внешний ключ старой категории ведёт на переименованную таблицу, каскад работает.
    ref = await db.fetchval(
        "SELECT confrelid::regclass::text FROM pg_constraint WHERE conname = 'categories_user_id_fkey'"
    )
    assert ref == "legacy_users"
    await db.execute("DELETE FROM legacy_users WHERE id = $1", user_id)
    assert await db.fetchval("SELECT count(*) FROM categories WHERE user_id = $1", user_id) == 0
    await alembic("check")


async def test_downgrade_restores_0003_names_and_upgrade_again(scratch_db):
    db, alembic = scratch_db
    await alembic("upgrade", "head")
    user_id = uuid.uuid4()
    await db.execute("INSERT INTO legacy_users (id, is_anonymous) VALUES ($1, true)", user_id)
    await alembic("downgrade", PREVIOUS)
    assert await revision(db) == PREVIOUS
    names = await tables(db)
    assert {"users", "refresh_tokens"} <= names
    assert not {"legacy_users", "devices", "sync_records"} & names
    assert await db.fetchval("SELECT count(*) FROM users WHERE id = $1", user_id) == 1
    indexes = {
        r["indexname"] for r in await db.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    }
    assert {"users_pkey", "refresh_tokens_pkey", "ix_refresh_tokens_user_id"} <= indexes
    await alembic("upgrade", "head")
    assert await revision(db) == HEAD
    assert await db.fetchval("SELECT count(*) FROM legacy_users WHERE id = $1", user_id) == 1
