"""Тестовый клиент: модель iOS local-first.

Локальная БД (SQLite-файл) — источник данных для «UI». Каждое изменение пишется в records и
outbox ОДНОЙ локальной транзакцией. Outbox, курсор и состояние HLC хранятся в том же файле,
поэтому переживают перезапуск процесса (новый `SyncClient` с тем же `path`).

Правила, которые клиент обязан соблюдать (их проверяют тесты):
1. Мутация удаляется из outbox только после ответа сервера.
2. Повтор после сбоя отправляет те же мутации с теми же mutationId, в том же порядке.
3. После `applied` более поздние мутации той же записи в outbox перебазируются на новую версию.
4. Входящая запись не перетирает локальную, если для неё есть неотправленная мутация: конфликт
   решит сервер, и ответ push вернёт авторитетное состояние.
5. Страница pull и новый курсор сохраняются одной локальной транзакцией.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# HLC нужен только для стратегий варианта A; для B клиент всё равно его ведёт (сервер игнорирует).
from syncproto import hlc as hlcmod

from .transport import Transport, TransportError

# Локальная схема: meta — служебные значения (device_id, cursor, состояние HLC);
# records — локальные записи (источник данных для «UI»), server_version — версия, на которой основана
# локальная копия; outbox — неотправленные мутации в порядке создания (seq).
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    payload TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
    server_version INTEGER NOT NULL DEFAULT 0, hlc TEXT,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mutation_id TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    body TEXT NOT NULL
);
"""


# Ошибка локального использования (правка несуществующей записи и т. п.).
class LocalError(Exception):
    pass


# Итоги одного sync() — для проверок в тестах и симуляции. ok=False — была ошибка сети.
@dataclass
class SyncReport:
    pushed: int = 0
    applied: int = 0
    rejected: int = 0
    conflicts: int = 0
    pulled: int = 0
    pages: int = 0
    push_requests: int = 0
    ok: bool = True


# Время в ISO 8601 с суффиксом Z.
def _iso(v: dt.datetime) -> str:
    return v.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


# Устройство: локальная БД + outbox + транспорт. Новый объект с тем же path = перезапуск приложения.
class SyncClient:
    def __init__(
        self,
        path: str,
        device_id: str,
        transport: Transport,
        clock: Callable[[], dt.datetime] | None = None,
        push_batch: int = 500,
        pull_limit: int = 500,
        ids: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self.device_id = device_id
        self.transport = transport
        # clock — часы устройства (в тестах — со сдвигом);
        # ids — генератор UUID (в симуляции — детерминированный).
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.push_batch = push_batch
        self.pull_limit = pull_limit
        self.new_id = ids or (lambda: str(uuid.uuid4()))
        # isolation_level=None — транзакции управляются явно через BEGIN/COMMIT.
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Файл БД привязан к одному устройству — защита от путаницы в тестах.
        stored = self._meta("device_id")
        if stored is None:
            self._set_meta("device_id", device_id)
        elif stored != device_id:
            raise LocalError("local DB belongs to another device")
        # HLC-часы с восстановленным из БД состоянием.
        self.hlc = hlcmod.HybridClock(
            lambda: hlcmod.to_ms(self.clock()), int(self._meta("hlc_l") or 0), int(self._meta("hlc_c") or 0)
        )

    def close(self) -> None:
        self.db.close()

    # --- meta -------------------------------------------------------------------------------

    # Чтение и запись служебных значений.
    def _meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v)
        )

    # Курсор pull — последняя полученная серверная версия.
    @property
    def cursor(self) -> int:
        return int(self._meta("cursor") or 0)

    # Сохранить состояние HLC (в той же транзакции, что и изменение).
    def _save_hlc(self) -> None:
        self._set_meta("hlc_l", str(self.hlc.l))
        self._set_meta("hlc_c", str(self.hlc.c))

    # --- локальные изменения ----------------------------------------------------------------

    # Создать категорию/расход: локальная запись + мутация в outbox. Возвращает id записи.
    def create_category(self, name: str, emoji: str | None = None, entity_id: str | None = None) -> str:
        payload = {"name": name, **({"emoji": emoji} if emoji else {})}
        return self._write("category", entity_id or self.new_id(), payload, create=True)

    def create_expense(
        self,
        amount: str,
        category_id: str,
        date: str,
        comment: str | None = None,
        entity_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {"amount": amount, "categoryId": category_id, "date": date}
        if comment is not None:
            payload["comment"] = comment
        return self._write("expense", entity_id or self.new_id(), payload, create=True)

    # Изменить поля записи (остальные поля сохраняются).
    def update(self, entity_type: str, entity_id: str, **changes: Any) -> None:
        row = self._record(entity_type, entity_id)
        if row is None or row["deleted_at"]:
            raise LocalError("record does not exist locally")
        payload = {**json.loads(row["payload"]), **changes}
        self._write(entity_type, entity_id, payload, create=False)

    # Удалить запись (локально остаётся tombstone).
    def delete(self, entity_type: str, entity_id: str) -> None:
        row = self._record(entity_type, entity_id)
        if row is None or row["deleted_at"]:
            raise LocalError("record does not exist locally")
        self._write(entity_type, entity_id, None, create=False)

    # Локальная запись по ключу или None.
    def _record(self, entity_type: str, entity_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM records WHERE entity_type = ? AND entity_id = ?", (entity_type, entity_id)
        ).fetchone()

    # Общая запись изменения: записи и outbox — одной локальной транзакцией (всё или ничего).
    def _write(self, entity_type: str, entity_id: str, payload: dict[str, Any] | None, create: bool) -> str:
        now = self.clock()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._record(entity_type, entity_id)
            if create and row is not None:
                raise LocalError("record already exists")
            # При правке время создания не меняется; база — серверная версия локальной копии.
            created_at = row["created_at"] if row else _iso(now)
            base = int(row["server_version"]) if row else 0
            stamp = self.hlc.tick()
            deleted_at = _iso(now) if payload is None else None
            # Обновить локальную запись. server_version при правке не трогаем: его меняет только сервер.
            self.db.execute(
                """INSERT INTO records (entity_type, entity_id, payload, created_at, updated_at, deleted_at,
                                        server_version, hlc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_type, entity_id) DO UPDATE SET payload = excluded.payload,
                       updated_at = excluded.updated_at, deleted_at = excluded.deleted_at,
                       hlc = excluded.hlc""",
                (
                    entity_type,
                    entity_id,
                    None if payload is None else json.dumps(payload),
                    created_at,
                    _iso(now),
                    deleted_at,
                    base,
                    stamp,
                ),
            )
            # Тело мутации — ровно в формате API; хранится в outbox до подтверждения сервером.
            body = {
                "mutationId": self.new_id(),
                "entityType": entity_type,
                "entityId": entity_id,
                "op": "delete" if payload is None else "upsert",
                "baseVersion": base,
                "hlc": stamp,
                "createdAt": created_at,
                "updatedAt": _iso(now),
                "deletedAt": deleted_at,
                "payload": payload,
            }
            self.db.execute(
                "INSERT INTO outbox (mutation_id, entity_type, entity_id, body) VALUES (?, ?, ?, ?)",
                (body["mutationId"], entity_type, entity_id, json.dumps(body)),
            )
            self._save_hlc()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return entity_id

    # --- sync -------------------------------------------------------------------------------

    # Сколько мутаций ждёт отправки.
    def outbox_size(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    # Полный цикл: сначала отправить свои изменения, потом получить чужие. Ошибка сети — ok=False.
    async def sync(self) -> SyncReport:
        report = SyncReport()
        try:
            await self.push_all(report)
            await self.pull_all(report)
        except TransportError:
            report.ok = False
        return report

    # Отправлять outbox пачками в порядке seq, пока он не опустеет.
    async def push_all(self, report: SyncReport | None = None) -> SyncReport:
        report = report or SyncReport()
        while True:
            rows = self.db.execute(
                "SELECT seq, mutation_id, body FROM outbox ORDER BY seq LIMIT ?", (self.push_batch,)
            ).fetchall()
            if not rows:
                return report
            body = {"deviceId": self.device_id, "mutations": [json.loads(r["body"]) for r in rows]}
            resp = await self.transport.push(body)  # TransportError → outbox не тронут
            report.push_requests += 1
            report.pushed += len(rows)
            self._apply_push(rows, resp["results"], report)

    # Обработать ответ push одной локальной транзакцией.
    def _apply_push(self, rows: list[sqlite3.Row], results: list[dict[str, Any]], report: SyncReport) -> None:
        by_mid = {r["mutation_id"]: r for r in rows}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for res in results:
                row = by_mid[res["mutationId"]]
                m = json.loads(row["body"])
                et, eid = m["entityType"], m["entityId"]
                # Сервер видел этот mutationId с другим телом — это ошибка клиента, падаем громко.
                if res["status"] == "rejected" and res["reason"] == "mutation_id_reused":
                    raise LocalError("server reports mutationId reuse: client bug")
                report.conflicts += int(res["conflict"])
                # Мутация обработана сервером — убираем из outbox.
                self.db.execute("DELETE FROM outbox WHERE seq = ?", (row["seq"],))
                # Более поздние неотправленные мутации той же записи.
                later = self.db.execute(
                    "SELECT seq, body FROM outbox WHERE entity_type = ? AND entity_id = ? AND seq > ?",
                    (et, eid, row["seq"]),
                ).fetchall()
                # Применена: запоминаем новую серверную версию записи.
                if res["status"] == "applied":
                    report.applied += 1
                    v = res["version"]
                    self.db.execute(
                        "UPDATE records SET server_version = ? WHERE entity_type = ? AND entity_id = ?",
                        (v, et, eid),
                    )
                    for lr in later:  # правило 3: перебазирование
                        b = json.loads(lr["body"])
                        b["baseVersion"] = v
                        self.db.execute(
                            "UPDATE outbox SET body = ? WHERE seq = ?", (json.dumps(b), lr["seq"])
                        )
                else:
                    report.rejected += int(res["status"] == "rejected")
                # Сервер прислал актуальную запись (конфликт, удаление, повтор) — применяем,
                # если по этой записи нет более новых своих правок.
                rec = res.get("record")
                if rec is not None and not later:
                    self._store_incoming(rec)
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # Получать страницы изменений после курсора, пока сервер говорит hasMore.
    async def pull_all(self, report: SyncReport | None = None) -> SyncReport:
        report = report or SyncReport()
        while True:
            page = await self.transport.pull(self.cursor, self.pull_limit)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for rec in page["records"]:
                    pending = self.db.execute(
                        "SELECT 1 FROM outbox WHERE entity_type = ? AND entity_id = ? LIMIT 1",
                        (rec["entityType"], rec["entityId"]),
                    ).fetchone()
                    # HLC учитывает все увиденные изменения (вариант A).
                    if rec.get("hlc"):
                        self.hlc.observe(rec["hlc"])
                    if pending is None:  # правило 4
                        self._store_incoming(rec)
                self._set_meta("cursor", str(page["nextCursor"]))  # правило 5
                self._save_hlc()
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            report.pulled += len(page["records"])
            report.pages += 1
            if not page["hasMore"]:
                return report

    # Применить серверную запись локально; условие WHERE не даёт откатить запись на более старую версию.
    def _store_incoming(self, rec: dict[str, Any]) -> None:
        if rec.get("hlc"):
            self.hlc.observe(rec["hlc"])
        self.db.execute(
            """INSERT INTO records (entity_type, entity_id, payload, created_at, updated_at, deleted_at,
                                    server_version, hlc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_id) DO UPDATE SET payload = excluded.payload,
                   created_at = excluded.created_at, updated_at = excluded.updated_at,
                   deleted_at = excluded.deleted_at, server_version = excluded.server_version,
                   hlc = excluded.hlc
               WHERE excluded.server_version >= records.server_version""",
            (
                rec["entityType"],
                rec["entityId"],
                None if rec["payload"] is None else json.dumps(rec["payload"]),
                rec["createdAt"],
                rec["updatedAt"],
                rec["deletedAt"],
                rec["version"],
                rec.get("hlc"),
            ),
        )

    # --- чтение для «UI» и проверок ---------------------------------------------------------

    # Видимые (не удалённые) записи типа — то, что показал бы «UI».
    def visible(self, entity_type: str) -> dict[str, dict[str, Any]]:
        rows = self.db.execute(
            "SELECT entity_id, payload FROM records WHERE entity_type = ? AND deleted_at IS NULL",
            (entity_type,),
        ).fetchall()
        return {r["entity_id"]: json.loads(r["payload"]) for r in rows}

    def snapshot(self) -> dict[tuple[str, str], tuple[Any, bool]]:
        """(тип, id) → (payload, удалена) — для сравнения с сервером и другими устройствами."""
        rows = self.db.execute("SELECT entity_type, entity_id, payload, deleted_at FROM records").fetchall()
        return {
            (r["entity_type"], r["entity_id"]): (
                None if r["deleted_at"] else json.loads(r["payload"]),
                r["deleted_at"] is not None,
            )
            for r in rows
        }
