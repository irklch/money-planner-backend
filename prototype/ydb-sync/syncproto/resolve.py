"""Правила разрешения конфликтов. Чистые функции: одинаково работают в памяти и поверх YDB.

Стратегии:
- `lww_clock`  — базовая линия без HLC: LWW по `updatedAt` клиента. Нужна, чтобы показать, что
                 именно исправляет HLC.
- `hlc`        — вариант A как в architecture-local-first.md §8.2: LWW по HLC, удаление — обычная
                 LWW-запись.
- `hlc_dw`     — вариант A + «удаление окончательно» (delete wins).
- `version`    — вариант B: серверная версия + `baseVersion` клиента, delete wins, конфликт
                 конкурентных правок решает `updatedAt` (с монотонной меткой записи).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from . import hlc as hlcmod
from .models import Mutation, StoredRecord

MAX_FUTURE = dt.timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Decision:
    apply: bool
    status: str  # applied | rejected | noop
    reason: str | None = None
    conflict: bool = False
    hlc: str | None = None
    order_ts: dt.datetime | None = None


class Strategy(Protocol):
    name: str
    needs_hlc: bool

    def decide(
        self, current: StoredRecord | None, m: Mutation, device_id: str, now: dt.datetime, base: int
    ) -> Decision: ...


def clamp_ts(ts: dt.datetime, now: dt.datetime) -> dt.datetime:
    return min(ts, now + MAX_FUTURE)


def _deleted_rules(current: StoredRecord, m: Mutation) -> Decision | None:
    """Общее правило delete wins: tombstone окончателен, UUID не переиспользуются."""
    if current.deleted:
        if m.op == "delete":
            return Decision(False, "noop")
        return Decision(False, "rejected", reason="deleted", conflict=True)
    return None


@dataclass(frozen=True)
class LwwStrategy:
    """LWW по записи. `clock="hlc"` — вариант A, `clock="wall"` — базовая линия."""

    name: str
    clock: str  # "hlc" | "wall"
    delete_wins: bool

    @property
    def needs_hlc(self) -> bool:
        return self.clock == "hlc"

    def _key(self, m: Mutation, device_id: str, now: dt.datetime) -> tuple[str, str]:
        if self.clock == "hlc":
            assert m.hlc is not None
            return (hlcmod.clamp(m.hlc, now + MAX_FUTURE), device_id)
        return (clamp_ts(m.updated_at, now).isoformat(), device_id)

    @staticmethod
    def _cur_key(current: StoredRecord, clock: str) -> tuple[str, str]:
        if clock == "hlc":
            return (current.hlc or "", current.device_id)
        return (current.order_ts.isoformat(), current.device_id)

    def decide(
        self, current: StoredRecord | None, m: Mutation, device_id: str, now: dt.datetime, base: int
    ) -> Decision:
        key = self._key(m, device_id, now)
        stored_hlc = key[0] if self.clock == "hlc" else None
        order_ts = clamp_ts(m.updated_at, now)
        if current is None:
            return Decision(True, "applied", hlc=stored_hlc, order_ts=order_ts)
        if self.delete_wins:
            d = _deleted_rules(current, m)
            if d:
                return d
            if m.op == "delete":
                return Decision(
                    True,
                    "applied",
                    hlc=max(stored_hlc or "", current.hlc or "") or None,
                    order_ts=max(order_ts, current.order_ts),
                )
        if current.mutation_id == m.mutation_id:
            return Decision(False, "noop")  # та же правка (повтор после истечения журнала)
        if current.device_id == device_id:
            # Правки одного устройства приходят в порядке outbox — следующая всегда новее.
            # Без этого правила обрезка будущего времени схлопывает их в одну метку и теряет.
            return Decision(
                True,
                "applied",
                hlc=max(stored_hlc or "", current.hlc or "") or None,
                order_ts=max(order_ts, current.order_ts),
            )
        cur = self._cur_key(current, self.clock)
        if key > cur:
            return Decision(True, "applied", hlc=stored_hlc, order_ts=order_ts)
        return Decision(False, "rejected", reason="stale", conflict=True)


@dataclass(frozen=True)
class VersionStrategy:
    """Вариант B.

    1. Запись удалена → любое изменение отклоняется (`deleted`), повторное удаление — `noop`.
    2. Удаление применяется всегда (delete wins).
    3. `baseVersion == current.version` (или текущая версия — предыдущая правка этого же
       устройства) → изменение основано на актуальном состоянии: применить. Часы не участвуют.
    4. Иначе это конкурентная правка (конфликт): побеждает больший (`updatedAt`, `deviceId`), где
       `updatedAt` обрезан до now+5 мин, а у текущей записи берётся её монотонная метка `order_ts`.
    """

    name: str = "version"
    needs_hlc: bool = False

    def decide(
        self, current: StoredRecord | None, m: Mutation, device_id: str, now: dt.datetime, base: int
    ) -> Decision:
        ts = clamp_ts(m.updated_at, now)
        if current is None:
            return Decision(True, "applied", order_ts=ts)
        d = _deleted_rules(current, m)
        if d:
            return d
        if current.mutation_id == m.mutation_id:
            return Decision(False, "noop")  # та же правка (повтор после истечения журнала)
        if m.op == "delete":
            return Decision(
                True, "applied", conflict=base != current.version, order_ts=max(ts, current.order_ts)
            )
        if base == current.version or current.device_id == device_id:
            # Своя предыдущая правка — тоже «актуальная основа»: outbox устройства FIFO.
            return Decision(True, "applied", order_ts=max(ts, current.order_ts))
        incoming, cur = (ts, device_id), (current.order_ts, current.device_id)
        if incoming > cur:
            return Decision(True, "applied", conflict=True, order_ts=ts)
        return Decision(False, "rejected", reason="conflict", conflict=True)


STRATEGIES: dict[str, Strategy] = {
    "lww_clock": LwwStrategy("lww_clock", clock="wall", delete_wins=False),
    "hlc": LwwStrategy("hlc", clock="hlc", delete_wins=False),
    "hlc_dw": LwwStrategy("hlc_dw", clock="hlc", delete_wins=True),
    "version": VersionStrategy(),
}
