"""Файлы миграций без YDB: имена, порядок, идемпотентность операторов, ожидаемая схема."""

import re

import pytest

from app.db_ydb.errors import MigrationError
from app.db_ydb.migrate import MIGRATIONS_DIR, load_migrations, split_statements

EXPECTED_TABLES = {
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


def _tables(stmts):
    return [re.search(r"`\{prefix\}/([a-z0-9_]+)`", s).group(1) for s in stmts]


def test_migrations_load_in_order():
    migs = load_migrations()
    assert [m.version for m in migs] == list(range(1, len(migs) + 1))
    assert migs[0].name == "initial"


def test_initial_schema_covers_architecture_tables():
    stmts = split_statements(load_migrations()[0].sql)
    assert set(_tables(stmts)) == EXPECTED_TABLES
    assert len(stmts) == len(EXPECTED_TABLES)


def test_statements_are_idempotent():
    for mig in load_migrations():
        for stmt in split_statements(mig.sql):
            assert stmt.startswith("CREATE TABLE IF NOT EXISTS"), stmt[:60]


def test_prefix_is_substituted():
    stmts = load_migrations()[0].statements("mp_staging")
    assert all("`mp_staging/" in s and "{prefix}" not in s for s in stmts)


def test_ttl_and_sync_index_as_planned():
    sql = load_migrations()[0].sql
    # Архитектура §7.1 / §8.1: TTL журнала мутаций 30 дней, доставок 7 дней; by_version — GLOBAL SYNC.
    assert re.search(r"sync_mutations.*?TTL = Interval\(\"P30D\"\) ON created_at", sql, re.S)
    assert re.search(r"import_deliveries.*?TTL = Interval\(\"P7D\"\) ON created_at", sql, re.S)
    assert re.search(r"rate_counters.*?TTL = Interval\(\"PT0S\"\) ON expires_at", sql, re.S)
    assert "INDEX by_version GLOBAL SYNC ON (user_id, version)" in sql
    assert "GLOBAL ASYNC" not in sql


def test_checksum_is_stable():
    a, b = load_migrations()[0], load_migrations()[0]
    assert a.checksum == b.checksum and len(a.checksum) == 64


def _write(tmp_path, files):
    for name, sql in files.items():
        (tmp_path / name).write_text(sql, encoding="utf-8")
    return tmp_path


OK = "CREATE TABLE IF NOT EXISTS `{prefix}/t` (id Utf8 NOT NULL, PRIMARY KEY (id));\n"


@pytest.mark.parametrize(
    "files,match",
    [
        ({"0001_a.sql": OK, "0003_b.sql": OK}, "without gaps"),
        ({"0002_a.sql": OK}, "without gaps"),
        ({"1_a.sql": OK}, "bad migration file name"),
        ({"0001_A.sql": OK}, "bad migration file name"),
        ({"0001_a.sql": "CREATE TABLE `{prefix}/t` (id Utf8 NOT NULL, PRIMARY KEY (id));"}, "IF NOT EXISTS"),
        ({"0001_a.sql": "DROP TABLE `{prefix}/t`;"}, "IF NOT EXISTS"),
        (
            {"0001_a.sql": "CREATE TABLE IF NOT EXISTS `other/t` (id Utf8, PRIMARY KEY (id));"},
            "IF NOT EXISTS",
        ),
    ],
)
def test_bad_migration_sets_rejected(tmp_path, files, match):
    with pytest.raises(MigrationError, match=match):
        load_migrations(_write(tmp_path, files))


def test_comments_and_multiple_statements(tmp_path):
    sql = "-- comment; with semicolon\n" + OK + "\n-- next\n" + OK.replace("/t`", "/u`")
    migs = load_migrations(_write(tmp_path, {"0001_two.sql": sql}))
    assert _tables(split_statements(migs[0].sql)) == ["t", "u"]


def test_migration_files_are_packaged():
    assert (MIGRATIONS_DIR / "0001_initial.sql").exists()
