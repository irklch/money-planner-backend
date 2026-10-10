"""Правила разрешения конфликтов (вариант B, утверждён). Чистая функция: одинаково работает в памяти
и поверх YDB.

Вариант B: серверная версия + `baseVersion` клиента, delete wins, archive wins для категорий.
HLC не используется: причинность доказывает `baseVersion`, а часы клиента нужны только для
выбора победителя между конкурентными правками.

Совпадение `deviceId` НЕ даёт приоритета. Правка применяется без конфликта, только если она
основана на актуальной версии записи (`baseVersion == current.version`). Последовательные
офлайн-правки одного устройства получают актуальную базу двумя способами:
- внутри одной пачки — перебазированием на сервере (engine.plan_push, `chain`);
- между пачками — перебазированием outbox на клиенте после ответа `applied` (client.py, правило 3).
"""

from __future__ import annotations

# Сюда смотреть, чтобы понять, ПОЧЕМУ сервер принял или отклонил правку.
import datetime as dt
from dataclasses import dataclass

from .models import Mutation, StoredRecord

# Насколько время клиента может быть впереди серверного; всё дальше обрезается до now + 5 мин.
MAX_FUTURE = dt.timedelta(minutes=5)


# Решение по одной мутации: применить или нет, статус/причина для ответа и метка для сохранения.
@dataclass(frozen=True, slots=True)
class Decision:
    apply: bool
    status: str  # applied | rejected | noop
    reason: str | None = None
    conflict: bool = False
    order_ts: dt.datetime | None = None


# Обрезать время клиента, если его часы спешат больше допуска.
def clamp_ts(ts: dt.datetime, now: dt.datetime) -> dt.datetime:
    return min(ts, now + MAX_FUTURE)


# Категория в архиве (удаление категории в продукте = архивирование).
def _archived(payload: dict | None) -> bool:
    return bool(payload and payload.get("isArchived"))


def decide(
    current: StoredRecord | None, m: Mutation, device_id: str, now: dt.datetime, base: int
) -> Decision:
    """Решение по мутации. `base` — baseVersion после перебазирования внутри пачки.

    1. Записи нет:
       - `base == 0` → новая запись, применить;
       - `base > 0` → клиент правит запись, которую сервер знал, а теперь её нет: tombstone уже
         очищен (удаление старше горизонта). Правка → `rejected/deleted`, удаление → `noop`.
         Иначе офлайн-правка после долгого отсутствия воскресила бы удалённый расход.
    2. Запись удалена → любое изменение отклоняется (`deleted`), повторное удаление — `noop`.
    3. Та же мутация уже дала текущее состояние → `noop` (повтор после истечения журнала).
    4. Удаление применяется всегда (delete wins).
    5. `base == current.version` → правка основана на актуальном состоянии: применить. Часы не участвуют.
    6. Иначе это конкурентная правка (конфликт):
       - категории: archive wins — архивирование побеждает, правка архивной категории без
         архивирования отклоняется (разархивирования в MVP нет);
       - остальное: побеждает больший (`updatedAt`, `deviceId`), где `updatedAt` обрезан до now+5 мин,
         а у текущей записи берётся её монотонная метка `order_ts`. deviceId здесь — только
         детерминированный разрыв ничьей, а не приоритет «своего» устройства.
    """
    # Время клиента с обрезкой будущего.
    ts = clamp_ts(m.updated_at, now)
    if current is None:
        if base == 0:
            return Decision(True, "applied", order_ts=ts)
        if m.op == "delete":
            return Decision(False, "noop")
        return Decision(False, "rejected", reason="deleted", conflict=True)
    # Удалённую запись не изменить и не воскресить.
    if current.deleted:
        if m.op == "delete":
            return Decision(False, "noop")
        return Decision(False, "rejected", reason="deleted", conflict=True)
    if current.mutation_id == m.mutation_id:
        return Decision(False, "noop")
    # Удаление побеждает всегда; conflict=true, если удаляли не последнюю версию (для статистики).
    if m.op == "delete":
        return Decision(True, "applied", conflict=base != current.version, order_ts=max(ts, current.order_ts))
    # Правка на актуальной версии: монотонная метка записи не откатывается назад.
    if base == current.version:
        return Decision(True, "applied", order_ts=max(ts, current.order_ts))
    # Конкурентная правка категории: архивирование окончательно, как удаление для расходов.
    if m.entity_type == "category":
        incoming_arch, current_arch = _archived(m.payload), _archived(current.payload)
        if incoming_arch and not current_arch:
            return Decision(True, "applied", conflict=True, order_ts=max(ts, current.order_ts))
        if current_arch and not incoming_arch:
            return Decision(False, "rejected", reason="conflict", conflict=True)
    # Конкурентная правка: сравниваем (время, устройство) с монотонной меткой текущей записи.
    if (ts, device_id) > (current.order_ts, current.device_id):
        return Decision(True, "applied", conflict=True, order_ts=ts)
    return Decision(False, "rejected", reason="conflict", conflict=True)
