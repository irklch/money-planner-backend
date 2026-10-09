"""Алгоритм push/pull, не зависящий от хранилища.

Push = прочитать снимок (состояние пользователя, журнал мутаций, текущие записи) → `plan_push`
(чистая функция) → записать план. Хранилище обязано выполнить чтение и запись в одной
serializable-транзакции и при конфликте транзакции повторить ВСЁ, включая планирование.
"""

from __future__ import annotations

# Здесь — сердце протокола. Хранилища (store_ydb.py, store_memory.py) только читают и пишут данные,
# а решения о каждой мутации принимает plan_push вместе с правилами из resolve.py.
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import Mutation, MutationResult, PushPlan, StoredRecord
from .resolve import decide


# Снимок состояния пользователя, прочитанный хранилищем в начале транзакции push.
@dataclass(slots=True)
class PushSnapshot:
    # Последняя выданная версия пользователя и горизонт очистки tombstones.
    last_version: int
    tombstone_horizon: int
    logs: dict[str, tuple[str, dict[str, Any]]]  # mutation_id -> (request_hash, stored result)
    records: dict[tuple[str, str], StoredRecord]


# Что стоила операция в YDB — для benchmark и заголовков ответа.
@dataclass(slots=True)
class OpStats:
    """Измерения одной операции хранилища (заполняет YDB-хранилище)."""

    # RU из заголовка x-ydb-consumed-units (локальная YDB не учитывает записи) и число обращений к YDB.
    ru: int = 0
    ydb_calls: int = 0
    attempts: int = 1  # >1 — были конфликты транзакций (ABORTED) и повторы
    calls_by_method: dict[str, int] = field(default_factory=dict)
    # Фактическая статистика YDB (stats_mode=BASIC), суммарно по запросам последней попытки.
    read_rows: int = 0
    read_bytes: int = 0
    write_rows: int = 0
    write_bytes: int = 0
    cpu_us: int = 0
    tables: dict[str, dict[str, int]] = field(default_factory=dict)
    # RU по официальной формуле из этих фактических строк/байт — ПРОГНОЗ, не измерение.
    ru_io_formula: int = 0


# Страница pull: записи, курсор для следующего запроса, есть ли ещё, текущий горизонт tombstones.
@dataclass(slots=True)
class PullPage:
    records: list[StoredRecord]
    next_cursor: int
    has_more: bool
    horizon: int = 0


class ResyncRequired(Exception):
    """Клиент мог пропустить очищенные tombstones: нужна полная перезагрузка."""


def check_horizon(cursor: int, known_horizon: int, horizon: int) -> None:
    """410, только если клиент реально мог пропустить удаление.

    Tombstones с версией <= horizon очищены. Клиент с курсором `cursor` видел всё до `cursor`.
    - cursor == 0 — загрузка с нуля: пропускать нечего;
    - cursor >= horizon — все очищенные tombstones клиент уже получил;
    - known_horizon >= horizon — горизонт не сдвигался с прошлого ответа клиенту (например, идёт
      постраничная загрузка с нуля, начатая уже после очистки): новых пропусков нет.
    Без третьего условия полная загрузка, начатая после очистки, получала 410 на второй странице,
    если живых записей ниже горизонта больше одной страницы, и зацикливалась (найдено симуляцией).
    """
    if cursor and cursor < horizon and known_horizon < horizon:
        raise ResyncRequired()


# Интерфейс хранилища: push по снимку и плану, pull страницей по курсору.
class Store(Protocol):
    async def push(
        self, user_id: str, mutations: list[Mutation], planner: Callable[[PushSnapshot], PushPlan]
    ) -> tuple[PushPlan, OpStats]: ...

    async def pull(
        self, user_id: str, cursor: int, limit: int, known_horizon: int = 0
    ) -> tuple[PullPage, OpStats]: ...


# Чистое планирование пачки: по снимку и мутациям решить, что записать и что ответить.
# Чистая — значит без обращений к БД: при повторе транзакции её можно безопасно вызвать ещё раз.
def plan_push(snap: PushSnapshot, mutations: list[Mutation], device_id: str, now: dt.datetime) -> PushPlan:
    # Текущая версия и состояние записей; меняются по ходу пачки (мутации видят результат предыдущих).
    version = snap.last_version
    current = dict(snap.records)
    plan = PushPlan(results=[], last_version=version)
    # Мутации, уже обработанные в этом же запросе (повтор id внутри одной пачки).
    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    # Перебазирование внутри одного запроса: клиент не знает версию, которую сервер присвоит
    # его первой правке, поэтому вторая правка той же записи в той же пачке несёт старую базу.
    chain: dict[tuple[str, str], tuple[int, int]] = {}  # key -> (база, полученная версия)

    for m in mutations:
        # Отпечаток мутации и её прежний результат, если она уже обрабатывалась (в пачке или раньше — журнал).
        h = m.request_hash(device_id)
        prior = seen.get(m.mutation_id) or snap.logs.get(m.mutation_id)
        cur = current.get(m.key)
        # Повтор мутации.
        if prior is not None:
            prior_hash, stored = prior
            # Тот же id, но другое содержимое — ошибка клиента, применять нельзя.
            if prior_hash != h:
                plan.results.append(
                    MutationResult(m.mutation_id, "rejected", reason="mutation_id_reused", record=cur)
                )
                continue
            r = MutationResult.from_stored(m.mutation_id, stored)
            # Повтор: возвращаем сохранённый результат и прикладываем актуальную запись, если она
            # изменилась с тех пор или мутация не была применена: иначе клиент «застрянет» на
            # своей версии (найдено симуляцией: повтор отклонённой правки после потери ответа).
            if cur is not None and (r.status != "applied" or cur.version != r.version):
                r.record = cur
            # Повтор применённой мутации тоже участвует в перебазировании следующих мутаций этой записи.
            # В журнале база уже перебазирована; исходная база цепочки сохраняется так же, как для
            # новых мутаций. Иначе после повтора [создание, правка] следующая офлайн-правка с базой 0
            # не попадала в цепочку и, если запись удалена и очищена, создавала её заново
            # (найдено симуляцией на 1000 историй).
            if r.status == "applied" and r.version is not None and r.base_version is not None:
                link = chain.get(m.key)
                origin = link[0] if link is not None and r.base_version == link[1] else r.base_version
                chain[m.key] = (origin, r.version)
            plan.results.append(r)
            continue

        # Новая мутация: перебазируем, если она основана на той же версии, что предыдущая правка из пачки.
        base = m.base_version
        link = chain.get(m.key)
        if link is not None and base == link[0]:
            base = link[1]

        # Решение по правилам варианта B.
        d = decide(cur, m, device_id, now, base)
        # Применяем: новая версия и новое состояние записи.
        if d.apply:
            version += 1
            rec = StoredRecord(
                entity_type=m.entity_type,
                entity_id=m.entity_id,
                version=version,
                # Время создания записи не меняется при правках.
                created_at=cur.created_at if cur else m.created_at,
                updated_at=m.updated_at,
                deleted_at=m.deleted_at if m.op == "delete" else None,
                order_ts=d.order_ts or m.updated_at,
                device_id=device_id,
                schema_version=m.schema_version,
                payload=None if m.op == "delete" else m.payload,
                mutation_id=m.mutation_id,
            )
            current[m.key] = rec
            plan.upserts[m.key] = rec
            # Исходная база сохраняется на всю цепочку: create → edit → edit в одной пачке.
            chain[m.key] = (link[0] if link is not None and base == link[1] else base, version)
            r = MutationResult(
                m.mutation_id, "applied", conflict=d.conflict, version=version, base_version=base
            )
        # Не применяем: возвращаем актуальную запись, чтобы клиент мог её применить.
        else:
            # Повтор применённой мутации после истечения журнала (текущее состояние — от неё):
            # следующие правки этой записи в пачке, основанные на той же базе, перебазируются.
            if d.status == "noop" and cur is not None and cur.mutation_id == m.mutation_id:
                chain[m.key] = (base, cur.version)
            r = MutationResult(
                m.mutation_id,
                d.status,
                reason=d.reason,
                conflict=d.conflict,
                version=cur.version if cur else None,
                record=cur,
            )
        # Результат каждой новой мутации пишется в журнал — для точного ответа на повтор.
        stored = r.stored_json()
        plan.new_logs.append((m.mutation_id, h, stored))
        seen[m.mutation_id] = (h, stored)
        plan.results.append(r)

    # Итог плана: последняя версия и нужно ли что-то записывать.
    plan.last_version = version
    plan.changed = bool(plan.upserts or plan.new_logs)
    return plan


# Фасад для API и тестов: хранилище + часы сервера (в симуляции — виртуальные).
class SyncEngine:
    def __init__(self, store: Store, clock: Callable[[], dt.datetime] | None = None) -> None:
        self.store = store
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def push(
        self, user_id: str, device_id: str, mutations: list[Mutation]
    ) -> tuple[list[MutationResult], OpStats]:
        # План строится внутри транзакции хранилища и при её повторе пересчитывается заново.
        def planner(snap: PushSnapshot) -> PushPlan:
            return plan_push(snap, mutations, device_id, self._clock())

        plan, stats = await self.store.push(user_id, mutations, planner)
        return plan.results, stats

    # Pull — просто чтение страницы из хранилища.
    async def pull(
        self, user_id: str, cursor: int, limit: int, known_horizon: int = 0
    ) -> tuple[PullPage, OpStats]:
        return await self.store.pull(user_id, cursor, limit, known_horizon)
