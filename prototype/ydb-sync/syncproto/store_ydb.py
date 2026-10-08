"""YDB-хранилище sync. Одна serializable-транзакция на push, snapshot-чтение на pull.

Push — ровно два обращения к YDB в штатном случае:
  1) чтение (начинает транзакцию): sync_state + sync_mutations по списку id + sync_records по
     списку ключей (lookup join, без сканирования);
  2) запись всех изменений + commit в том же запросе.
Если все мутации — повторы, вместо записи делается rollback (ничего не меняется).

Версии назначаются в Python по прочитанному `sync_state.last_version`. Это безопасно: YDB
(OCC) отменит транзакцию с ABORTED, если строку sync_state успел изменить конкурентный push,
и мы повторим всё целиком. Поэтому порядок версий = порядок коммитов, и pull по курсору не
может «перепрыгнуть» ещё не закоммиченную меньшую версию.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

import ydb
import ydb.iam
from ydb.retries import retry_operation_async

from . import metering
from .config import Settings
from .engine import OpStats, PullPage, PushSnapshot, ResyncRequired
from .models import Mutation, PushPlan, StoredRecord
from .schema import COVER_COLUMNS

T = ydb.PrimitiveType
_KEY_T = ydb.StructType().add_member("entity_type", T.Utf8).add_member("entity_id", T.Utf8)
_REC_T = ydb.StructType()
for _name, _type in (
    ("user_id", T.Utf8),
    ("entity_type", T.Utf8),
    ("entity_id", T.Utf8),
    ("version", T.Uint64),
    ("created_at", T.Timestamp),
    ("updated_at", T.Timestamp),
    ("deleted_at", ydb.OptionalType(T.Timestamp)),
    ("order_ts", T.Timestamp),
    ("hlc", ydb.OptionalType(T.Utf8)),
    ("device_id", T.Utf8),
    ("schema_version", T.Uint32),
    ("mutation_id", T.Utf8),
    ("payload", ydb.OptionalType(T.Json)),
    ("server_updated_at", T.Timestamp),
):
    _REC_T.add_member(_name, _type)
_LOG_T = (
    ydb.StructType()
    .add_member("user_id", T.Utf8)
    .add_member("mutation_id", T.Utf8)
    .add_member("request_hash", T.Utf8)
    .add_member("result", T.Json)
    .add_member("created_at", T.Timestamp)
)

_REC_DECL = (
    "List<Struct<user_id:Utf8, entity_type:Utf8, entity_id:Utf8, version:Uint64, created_at:Timestamp,"
    " updated_at:Timestamp, deleted_at:Timestamp?, order_ts:Timestamp, hlc:Utf8?, device_id:Utf8,"
    " schema_version:Uint32, mutation_id:Utf8, payload:Json?, server_updated_at:Timestamp>>"
)
_SELECT_COLS = ", ".join(("entity_type", "entity_id", "version", *COVER_COLUMNS))


def _utc(v: dt.datetime | None) -> dt.datetime | None:
    if v is None:
        return None
    return v.replace(tzinfo=dt.UTC) if v.tzinfo is None else v.astimezone(dt.UTC)


def _payload(v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    return json.loads(v) if isinstance(v, str | bytes) else v


def _row_to_record(row: Any) -> StoredRecord:
    return StoredRecord(
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        version=int(row["version"]),
        created_at=_utc(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_utc(row["updated_at"]),  # type: ignore[arg-type]
        deleted_at=_utc(row["deleted_at"]),
        hlc=row["hlc"],
        order_ts=_utc(row["order_ts"]),  # type: ignore[arg-type]
        device_id=row["device_id"],
        schema_version=int(row["schema_version"]),
        payload=_payload(row["payload"]),
        mutation_id=row["mutation_id"],
    )


def _record_to_row(user_id: str, r: StoredRecord, now: dt.datetime) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "version": r.version,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "deleted_at": r.deleted_at,
        "order_ts": r.order_ts,
        "hlc": r.hlc,
        "device_id": r.device_id,
        "schema_version": r.schema_version,
        "mutation_id": r.mutation_id,
        "payload": None
        if r.payload is None
        else json.dumps(r.payload, ensure_ascii=False, separators=(",", ":")),
        "server_updated_at": now,
    }


async def open_driver(s: Settings) -> ydb.aio.Driver:
    metering.install()
    if s.ydb_auth == "anonymous":
        creds: Any = ydb.AnonymousCredentials()
    elif s.ydb_auth == "metadata":
        # IAM-токен сервисного аккаунта из сервиса метаданных (Serverless Containers / VM).
        creds = ydb.iam.MetadataUrlCredentials()
    elif s.ydb_auth == "env":
        # YDB_ACCESS_TOKEN_CREDENTIALS=$(yc iam create-token) — для создания схемы оператором.
        creds = ydb.credentials_from_env_variables()
    else:
        creds = ydb.iam.ServiceAccountCredentials.from_file(s.ydb_sa_key_file)
    driver = ydb.aio.Driver(endpoint=s.ydb_endpoint, database=s.ydb_database, credentials=creds)
    await driver.wait(timeout=15, fail_fast=True)
    return driver


class YdbStore:
    def __init__(
        self,
        pool: ydb.aio.QuerySessionPool,
        prefix: str,
        *,
        mutation_log: bool = True,
        key_lookup: str = "join",
        collect_stats: bool = False,
    ) -> None:
        self.pool = pool
        self.collect_stats = collect_stats
        self.prefix = prefix
        self.mutation_log = mutation_log
        self.key_lookup = key_lookup
        p = prefix
        if key_lookup == "join":
            records_read = f"""
            SELECT {", ".join(f"r.{c} AS {c}" for c in _SELECT_COLS.split(", "))}
            FROM AS_TABLE($keys) AS k
            INNER JOIN `{p}/sync_records` AS r
                ON r.entity_type = k.entity_type AND r.entity_id = k.entity_id
            WHERE r.user_id = $user_id;"""
            keys_decl = "DECLARE $keys AS List<Struct<entity_type:Utf8, entity_id:Utf8>>;"
        else:
            records_read = f"""
            SELECT {_SELECT_COLS} FROM `{p}/sync_records`
            WHERE user_id = $user_id AND (entity_type, entity_id) IN $keys;"""
            keys_decl = "DECLARE $keys AS List<Tuple<Utf8, Utf8>>;"
        self._q_read = f"""
            DECLARE $user_id AS Utf8;
            DECLARE $mids AS List<Utf8>;
            {keys_decl}
            SELECT last_version, tombstone_horizon FROM `{p}/sync_state` WHERE user_id = $user_id;
            SELECT mutation_id, request_hash, result FROM `{p}/sync_mutations`
            WHERE user_id = $user_id AND mutation_id IN $mids;
            {records_read}
        """
        log_write = f"UPSERT INTO `{p}/sync_mutations` SELECT * FROM AS_TABLE($logs);" if mutation_log else ""
        self._q_write = f"""
            DECLARE $user_id AS Utf8;
            DECLARE $records AS {_REC_DECL};
            DECLARE $logs AS List<Struct<user_id:Utf8, mutation_id:Utf8, request_hash:Utf8, result:Json,
                                         created_at:Timestamp>>;
            DECLARE $last_version AS Uint64;
            DECLARE $horizon AS Uint64;
            DECLARE $now AS Timestamp;
            UPSERT INTO `{p}/sync_records` SELECT * FROM AS_TABLE($records);
            {log_write}
            UPSERT INTO `{p}/sync_state` (user_id, last_version, tombstone_horizon, updated_at)
            VALUES ($user_id, $last_version, $horizon, $now);
        """
        self._q_pull = f"""
            DECLARE $user_id AS Utf8;
            DECLARE $cursor AS Uint64;
            DECLARE $limit AS Uint64;
            SELECT tombstone_horizon FROM `{p}/sync_state` WHERE user_id = $user_id;
            SELECT {_SELECT_COLS} FROM `{p}/sync_records` VIEW by_version
            WHERE user_id = $user_id AND version > $cursor
            ORDER BY version
            LIMIT $limit;
        """

    async def _exec(
        self, tx: Any, stats: OpStats, query: str, params: dict[str, Any], commit: bool = False
    ) -> list[list[Any]]:
        mode = ydb.QueryStatsMode.BASIC if self.collect_stats else None
        sets = await self._collect(await tx.execute(query, params, commit_tx=commit, stats_mode=mode))
        if self.collect_stats and tx.last_query_stats is not None:
            _account(tx.last_query_stats, stats)
        return sets

    @staticmethod
    async def _collect(it: Any) -> list[list[Any]]:
        out: list[list[Any]] = []
        async with it as results:
            async for rs in results:
                if rs is None:  # части стрима только со статистикой
                    continue
                # Результат одного SELECT может прийти несколькими частями с одним индексом.
                idx = rs.index if rs.index is not None else len(out)
                while len(out) <= idx:
                    out.append([])
                out[idx].extend(rs.rows)
        return out

    async def push(
        self, user_id: str, mutations: list[Mutation], planner: Callable[[PushSnapshot], PushPlan]
    ) -> tuple[PushPlan, OpStats]:
        stats = OpStats(attempts=0)
        keys = sorted({m.key for m in mutations})
        if self.key_lookup == "join":
            keys_param = ([{"entity_type": t, "entity_id": i} for t, i in keys], ydb.ListType(_KEY_T))
        else:
            kt = ydb.TupleType().add_element(T.Utf8).add_element(T.Utf8)
            keys_param = (keys, ydb.ListType(kt))
        read_params = {
            "$user_id": (user_id, T.Utf8),
            "$mids": ([m.mutation_id for m in mutations], ydb.ListType(T.Utf8)),
            "$keys": keys_param,
        }

        async def attempt(session: ydb.aio.QuerySession) -> PushPlan:
            stats.attempts += 1
            tx = session.transaction(ydb.QuerySerializableReadWrite())
            _reset_io(stats)
            sets = await self._exec(tx, stats, self._q_read, read_params)
            state = sets[0][0] if sets and sets[0] else None
            snap = PushSnapshot(
                last_version=int(state["last_version"]) if state else 0,
                tombstone_horizon=int(state["tombstone_horizon"]) if state else 0,
                logs={
                    r["mutation_id"]: (r["request_hash"], _payload(r["result"]))  # type: ignore[misc]
                    for r in (sets[1] if len(sets) > 1 else [])
                },
                records={
                    (r["entity_type"], r["entity_id"]): _row_to_record(r)
                    for r in (sets[2] if len(sets) > 2 else [])
                },
            )
            plan = planner(snap)
            if not plan.changed:
                await tx.rollback()
                return plan
            now = dt.datetime.now(dt.UTC)
            params = {
                "$user_id": (user_id, T.Utf8),
                "$records": (
                    [_record_to_row(user_id, r, now) for r in plan.upserts.values()],
                    ydb.ListType(_REC_T),
                ),
                "$logs": (
                    [
                        {
                            "user_id": user_id,
                            "mutation_id": mid,
                            "request_hash": h,
                            "result": json.dumps(res, separators=(",", ":")),
                            "created_at": now,
                        }
                        for mid, h, res in plan.new_logs
                    ],
                    ydb.ListType(_LOG_T),
                ),
                "$last_version": (plan.last_version, T.Uint64),
                "$horizon": (snap.tombstone_horizon, T.Uint64),
                "$now": (now, T.Timestamp),
            }
            await self._exec(tx, stats, self._q_write, params, commit=True)
            return plan

        plan = await self._run(attempt, stats)
        return plan, stats

    async def pull(self, user_id: str, cursor: int, limit: int) -> tuple[PullPage, OpStats]:
        stats = OpStats(attempts=0)
        params = {
            "$user_id": (user_id, T.Utf8),
            "$cursor": (cursor, T.Uint64),
            "$limit": (limit + 1, T.Uint64),  # +1 — чтобы точно знать has_more
        }

        async def attempt(session: ydb.aio.QuerySession) -> list[list[Any]]:
            stats.attempts += 1
            # Snapshot RO: состояние и страница записей — из одного согласованного снимка.
            tx = session.transaction(ydb.QuerySnapshotReadOnly())
            _reset_io(stats)
            return await self._exec(tx, stats, self._q_pull, params, commit=True)

        sets = await self._run(attempt, stats)
        state = sets[0][0] if sets and sets[0] else None
        if cursor and state and cursor < int(state["tombstone_horizon"]):
            raise ResyncRequired()
        rows = [_row_to_record(r) for r in (sets[1] if len(sets) > 1 else [])]
        page, has_more = rows[:limit], len(rows) > limit
        return PullPage(page, page[-1].version if page else cursor, has_more), stats

    async def _run(self, attempt: Callable[[Any], Any], stats: OpStats) -> Any:
        settings = ydb.RetrySettings(max_retries=15, idempotent=True)
        with metering.metered() as meter:

            async def callee() -> Any:
                async with self.pool.checkout() as session:
                    return await attempt(session)

            try:
                return await retry_operation_async(callee, settings)
            finally:
                await meter.settle()
                stats.ru = meter.ru
                stats.ydb_calls = meter.ydb_calls
                stats.calls_by_method = dict(meter.calls)

    # --- служебное для тестов и benchmark ---------------------------------------------------

    async def wipe_users(self, user_ids: list[str]) -> None:
        q = f"""
            DECLARE $ids AS List<Utf8>;
            DELETE FROM `{self.prefix}/sync_records` WHERE user_id IN $ids;
            DELETE FROM `{self.prefix}/sync_state` WHERE user_id IN $ids;
            DELETE FROM `{self.prefix}/sync_mutations` WHERE user_id IN $ids;
        """
        await self.pool.execute_with_retries(q, {"$ids": (user_ids, ydb.ListType(T.Utf8))})

    async def set_tombstone_horizon(self, user_id: str, horizon: int) -> None:
        q = f"""
            DECLARE $u AS Utf8; DECLARE $h AS Uint64;
            UPDATE `{self.prefix}/sync_state` SET tombstone_horizon = $h WHERE user_id = $u;
        """
        await self.pool.execute_with_retries(q, {"$u": (user_id, T.Utf8), "$h": (horizon, T.Uint64)})

    async def server_state(self, user_id: str) -> dict[tuple[str, str], StoredRecord]:
        q = f"""
            DECLARE $u AS Utf8;
            SELECT {_SELECT_COLS} FROM `{self.prefix}/sync_records` WHERE user_id = $u;
        """
        sets = await self.pool.execute_with_retries(q, {"$u": (user_id, T.Utf8)})
        return {(r["entity_type"], r["entity_id"]): _row_to_record(r) for rs in sets for r in rs.rows}

    async def last_version(self, user_id: str) -> int:
        q = f"DECLARE $u AS Utf8; SELECT last_version FROM `{self.prefix}/sync_state` WHERE user_id = $u;"
        sets = await self.pool.execute_with_retries(q, {"$u": (user_id, T.Utf8)})
        return int(sets[0].rows[0]["last_version"]) if sets and sets[0].rows else 0


def _reset_io(stats: OpStats) -> None:
    stats.read_rows = stats.read_bytes = stats.write_rows = stats.write_bytes = stats.cpu_us = 0
    stats.ru_io_formula = 0
    stats.tables = {}


def _account(qs: Any, stats: OpStats) -> None:
    """Строки/байты по таблицам (включая индексные) и CPU одного запроса + RU по формуле
    https://yandex.cloud/ru/docs/ydb/pricing/ru-yql (только ввод-вывод: CPU локальной YDB под
    эмуляцией amd64 не репрезентативен для Serverless)."""
    r_rows = r_bytes = w_rows = w_bytes = d_rows = 0
    cpu = int(getattr(qs.compilation, "cpu_time_us", 0) or 0)
    for phase in qs.query_phases:
        cpu += int(phase.cpu_time_us)
        for ta in phase.table_access:
            t = stats.tables.setdefault(
                ta.name.rsplit("/", 2)[-1] if "indexImplTable" not in ta.name else "by_version(index)",
                {"read_rows": 0, "write_rows": 0, "write_bytes": 0},
            )
            t["read_rows"] += int(ta.reads.rows)
            t["write_rows"] += int(ta.updates.rows) + int(ta.deletes.rows)
            t["write_bytes"] += int(ta.updates.bytes)
            r_rows += int(ta.reads.rows)
            r_bytes += int(ta.reads.bytes)
            w_rows += int(ta.updates.rows)
            w_bytes += int(ta.updates.bytes)
            d_rows += int(ta.deletes.rows)
    stats.read_rows += r_rows
    stats.read_bytes += r_bytes
    stats.write_rows += w_rows + d_rows
    stats.write_bytes += w_bytes
    stats.cpu_us += cpu
    reads = max(r_rows, -(-r_bytes // 4096))
    writes = max(w_rows, -(-w_bytes // 1024)) + d_rows
    stats.ru_io_formula += reads + 2 * writes
