"""Версионированные YQL-миграции.

    python -m app.db_ydb.migrate            # применить новые миграции
    python -m app.db_ydb.migrate --status   # список: применена / ожидает
    python -m app.db_ydb.migrate --check    # exit 1, если есть неприменённые или изменённые миграции

Файлы — `app/db_ydb/migrations/NNNN_name.sql`, версии подряд с 1. Применённые записываются в
`{prefix}/schema_migrations` с SHA-256 файла: изменённый после применения файл — ошибка (схему
меняет только новая миграция). DDL в YDB не транзакционен, поэтому операторы миграций
идемпотентны (`CREATE TABLE IF NOT EXISTS`): прерванная миграция повторяется целиком.

Миграции запускаются отдельным шагом (оператор / CI деплоя, отдельный сервисный аккаунт с правом
DDL — план §10), а не при старте контейнера: холодный старт не тратит время и RU на схему.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import ydb

from app.db_ydb.client import YdbClient
from app.db_ydb.errors import MigrationError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_ALLOWED_STATEMENT = re.compile(r"^CREATE TABLE IF NOT EXISTS `\{prefix\}/[a-z0-9_]+` \(", re.S)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def statements(self, prefix: str) -> list[str]:
        return [s.replace("{prefix}", prefix) for s in split_statements(self.sql)]


def split_statements(sql: str) -> list[str]:
    """Операторы через `;` в конце строки; комментарии `--` отбрасываются."""
    lines = [ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")]
    parts = re.split(r";\s*(?:\n|$)", "\n".join(lines))
    return [p.strip() for p in parts if p.strip()]


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        m = _FILE_RE.match(path.name)
        if not m:
            raise MigrationError(f"bad migration file name: {path.name}")
        found.append(Migration(int(m.group(1)), m.group(2), path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must be 1..N without gaps: {versions}")
    for mig in found:
        for stmt in split_statements(mig.sql):
            if not _ALLOWED_STATEMENT.match(stmt):
                raise MigrationError(f"{mig.version:04d}: only CREATE TABLE IF NOT EXISTS is allowed")
    return found


@dataclass(frozen=True)
class Applied:
    version: int
    name: str
    checksum: str


class Migrator:
    def __init__(self, client: YdbClient, migrations: list[Migration] | None = None):
        self.client = client
        self.migrations = load_migrations() if migrations is None else migrations
        self.table = client.table("schema_migrations")

    async def _ensure_table(self) -> None:
        await self.client.ddl(
            f"""CREATE TABLE IF NOT EXISTS `{self.table}` (
                version Uint32 NOT NULL,
                name Utf8 NOT NULL,
                checksum Utf8 NOT NULL,
                applied_at Timestamp NOT NULL,
                PRIMARY KEY (version)
            )"""
        )

    async def applied(self) -> dict[int, Applied]:
        await self._ensure_table()
        sets = await self.client.execute(
            f"SELECT version, name, checksum FROM `{self.table}` ORDER BY version;", idempotent=True
        )
        rows = sets[0] if sets else []
        return {int(r["version"]): Applied(int(r["version"]), r["name"], r["checksum"]) for r in rows}

    async def status(self) -> list[tuple[Migration, Applied | None]]:
        done = await self.applied()
        for version, rec in done.items():
            mig = next((m for m in self.migrations if m.version == version), None)
            if mig is None:
                raise MigrationError(f"{version:04d} is applied but its file is missing")
            if mig.checksum != rec.checksum:
                raise MigrationError(f"{version:04d}_{mig.name} changed after it was applied")
        return [(m, done.get(m.version)) for m in self.migrations]

    async def pending(self) -> list[Migration]:
        return [m for m, rec in await self.status() if rec is None]

    async def migrate(self) -> list[Migration]:
        """Применить ожидающие миграции по порядку. Повторный запуск безопасен."""
        applied_now = []
        for mig in await self.pending():
            for stmt in mig.statements(self.client.options.prefix):
                await self.client.ddl(stmt)
            await self.client.execute(
                f"""DECLARE $version AS Uint32; DECLARE $name AS Utf8; DECLARE $checksum AS Utf8;
                DECLARE $now AS Timestamp;
                UPSERT INTO `{self.table}` (version, name, checksum, applied_at)
                VALUES ($version, $name, $checksum, $now);""",
                {
                    "$version": (mig.version, ydb.PrimitiveType.Uint32),
                    "$name": (mig.name, ydb.PrimitiveType.Utf8),
                    "$checksum": (mig.checksum, ydb.PrimitiveType.Utf8),
                    "$now": (dt.datetime.now(dt.UTC), ydb.PrimitiveType.Timestamp),
                },
                idempotent=True,
            )
            applied_now.append(mig)
        return applied_now


async def _main(argv: list[str]) -> int:
    from app.core.config import get_settings
    from app.db_ydb.client import open_client

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    client = await open_client(get_settings())
    try:
        migrator = Migrator(client)
        if args.status or args.check:
            rows = await migrator.status()
            for mig, rec in rows:
                print(f"{mig.version:04d}_{mig.name}: {'applied' if rec else 'pending'}")
            pending = [m for m, rec in rows if rec is None]
            return 1 if args.check and pending else 0
        done = await migrator.migrate()
        print(f"applied: {', '.join(f'{m.version:04d}_{m.name}' for m in done) or 'nothing (up to date)'}")
        return 0
    except MigrationError as e:
        print(f"migration error: {e}", file=sys.stderr)
        return 2
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
