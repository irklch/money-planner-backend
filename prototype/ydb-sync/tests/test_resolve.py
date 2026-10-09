"""Модульные тесты правил разрешения конфликтов и планирования пачки (без YDB)."""

from __future__ import annotations

import datetime as dt

from syncproto.engine import PushSnapshot, plan_push
from syncproto.models import Mutation, StoredRecord
from syncproto.resolve import MAX_FUTURE, decide

# Фиксированное «сейчас» сервера для детерминированных проверок.
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)


# Текущая запись на сервере с заданной версией, временем и устройством.
def rec(
    version: int,
    ts: dt.datetime,
    device: str = "d1",
    deleted: bool = False,
    mid: str = "m0",
    entity_type: str = "expense",
    payload: dict | None = None,
):
    return StoredRecord(
        entity_type,
        "e1",
        version,
        NOW,
        ts,
        ts if deleted else None,
        ts,
        device,
        1,
        None if deleted else (payload or {"amount": "1"}),
        mid,
    )


# Входящая мутация с заданным временем и базовой версией.
def mut(
    ts: dt.datetime,
    base: int = 0,
    op: str = "upsert",
    mid: str = "m1",
    entity_type: str = "expense",
    payload: dict | None = None,
):
    return Mutation(
        mid,
        entity_type,
        "e1",
        op,
        base,
        NOW,
        ts,
        ts if op == "delete" else None,
        1,
        None if op == "delete" else (payload or {"amount": "2"}),
    )


# Правка на актуальной версии применяется, даже если часы устройства отстают на год.
def test_base_match_applies_even_with_clock_far_behind():
    cur = rec(5, NOW)
    d = decide(cur, mut(NOW - dt.timedelta(days=365), base=5), "d2", NOW, 5)
    assert d.apply and not d.conflict
    assert d.order_ts == NOW  # монотонная метка не откатывается назад


# Конкурентный конфликт решают время, затем устройство (только как разрыв ничьей).
def test_conflict_decided_by_time_then_device():
    cur = rec(5, NOW)
    assert decide(cur, mut(NOW + dt.timedelta(seconds=1), base=4), "d2", NOW, 4).apply
    assert not decide(cur, mut(NOW - dt.timedelta(seconds=1), base=4), "d2", NOW, 4).apply
    assert decide(cur, mut(NOW, base=4), "d2", NOW, 4).apply  # ничья → больший deviceId
    assert not decide(rec(5, NOW, device="d3"), mut(NOW, base=4), "d2", NOW, 4).apply


def test_same_device_stale_independent_edit_is_rejected():
    """Регрессия: раньше совпадение deviceId с автором последней правки давало приоритет, и
    устаревшая независимая правка того же устройства (база 4, время раньше) перезаписывала
    актуальную версию 5. Теперь это обычный конкурентный конфликт: проигрывает более старая."""
    cur = rec(5, NOW, device="d1")
    d = decide(cur, mut(NOW - dt.timedelta(minutes=10), base=4), "d1", NOW, 4)
    assert not d.apply and d.status == "rejected" and d.reason == "conflict" and d.conflict


def test_same_device_gets_no_priority_on_tie():
    """Та же метка времени и то же устройство — не «своя правка новее», а ничья: текущее остаётся."""
    cur = rec(5, NOW, device="d1")
    assert not decide(cur, mut(NOW, base=4), "d1", NOW, 4).apply


def test_same_device_sequential_edit_on_current_base_applies():
    """Последовательная правка того же устройства, основанная на актуальной версии, применяется
    без конфликта — даже если часы устройства убежали вперёд и обе метки обрезаны до now+5 мин."""
    far = NOW + dt.timedelta(days=1)
    cur = rec(5, NOW + MAX_FUTURE, device="d1")
    d = decide(cur, mut(far + dt.timedelta(seconds=1), base=5), "d1", NOW, 5)
    assert d.apply and not d.conflict


# Время из будущего обрезается до now + 5 мин.
def test_future_clock_is_clamped():
    cur = rec(5, NOW + dt.timedelta(minutes=4))
    d = decide(cur, mut(NOW + dt.timedelta(days=1), base=4), "d2", NOW, 4)
    assert d.apply and d.order_ts == NOW + MAX_FUTURE


# Delete wins: удаление проходит, удалённую запись нельзя изменить, повторное удаление — noop.
def test_delete_wins_and_tombstone_is_final():
    cur = rec(5, NOW)
    assert decide(cur, mut(NOW - dt.timedelta(hours=1), base=3, op="delete"), "d2", NOW, 3).apply
    dead = rec(6, NOW, deleted=True)
    later_edit = decide(dead, mut(NOW + dt.timedelta(minutes=1), base=5), "d3", NOW, 5)
    assert not later_edit.apply and later_edit.reason == "deleted"
    assert decide(dead, mut(NOW, base=6, op="delete"), "d3", NOW, 6).status == "noop"


def test_edit_of_purged_record_is_rejected_not_resurrected():
    """Tombstone очищен (запись удалена давно). Офлайн-правка с baseVersion > 0 не создаёт запись
    заново; новая запись (baseVersion 0) создаётся как обычно."""
    d = decide(None, mut(NOW, base=7), "d1", NOW, 7)
    assert not d.apply and d.reason == "deleted"
    assert decide(None, mut(NOW, base=7, op="delete"), "d1", NOW, 7).status == "noop"
    assert decide(None, mut(NOW, base=0), "d1", NOW, 0).apply


# Та же мутация, уже давшая текущее состояние, — noop (повтор после истечения журнала).
def test_same_mutation_is_noop():
    cur = rec(5, NOW, mid="m1")
    assert decide(cur, mut(NOW, base=4, mid="m1"), "d1", NOW, 4).status == "noop"


def test_category_archive_wins_over_concurrent_edit():
    """Архивирование категории окончательно: конкурентное переименование его не отменяет,
    а архивирование побеждает более позднюю конкурентную правку."""
    active = {"name": "Еда", "isArchived": False}
    archived = {"name": "Еда", "isArchived": True}
    renamed = {"name": "Продукты", "isArchived": False}
    # Текущая — в архиве, конкурентное переименование (даже более позднее) отклоняется.
    cur = rec(5, NOW, entity_type="category", payload=archived)
    m = mut(NOW + dt.timedelta(minutes=1), base=4, entity_type="category", payload=renamed)
    assert not decide(cur, m, "d2", NOW, 4).apply
    # Текущая — переименована позже, конкурентное архивирование всё равно применяется.
    cur = rec(5, NOW + dt.timedelta(minutes=1), entity_type="category", payload=renamed)
    m = mut(NOW, base=4, entity_type="category", payload=archived)
    assert decide(cur, m, "d2", NOW, 4).apply
    # Неконфликтная правка архивной категории (на актуальной базе) — обычная правка.
    cur = rec(5, NOW, entity_type="category", payload=archived)
    m = mut(NOW, base=5, entity_type="category", payload={**archived, "emoji": "🍔"})
    assert decide(cur, m, "d2", NOW, 5).apply
    # Конкурентная правка двух активных — обычный LWW.
    cur = rec(5, NOW, entity_type="category", payload=active)
    assert decide(
        cur, mut(NOW + dt.timedelta(seconds=1), base=4, entity_type="category", payload=renamed), "d2", NOW, 4
    ).apply


# --- планирование пачки -------------------------------------------------------------------


def _snap(*records: StoredRecord, last: int = 10, logs: dict | None = None) -> PushSnapshot:
    return PushSnapshot(last, 0, logs or {}, {r.key: r for r in records})


def test_offline_edits_of_one_device_in_one_batch_are_rebased():
    """Устройство офлайн дважды правит запись версии 5: обе мутации несут baseVersion 5.
    В одной пачке вторая перебазируется на версию, выданную первой, и применяется без конфликта."""
    cur = rec(5, NOW, device="d2")
    m1 = mut(NOW + dt.timedelta(seconds=1), base=5, mid="a")
    m2 = mut(NOW + dt.timedelta(seconds=2), base=5, mid="b", payload={"amount": "3"})
    plan = plan_push(_snap(cur), [m1, m2], "d1", NOW)
    assert [r.status for r in plan.results] == ["applied", "applied"]
    assert not any(r.conflict for r in plan.results)
    assert plan.upserts[("expense", "e1")].payload == {"amount": "3"}


def test_replay_after_log_expiry_still_rebases_next_edit():
    """Журнал мутаций истёк, ответ на правку `a` был потерян. Повтор `a` — noop (её mutation_id в
    записи), а следующая офлайн-правка `b` с той же старой базой перебазируется и применяется."""
    cur = rec(6, NOW, device="d1", mid="a")  # состояние получено правкой a (база 5)
    m1 = mut(NOW, base=5, mid="a")
    m2 = mut(NOW - dt.timedelta(minutes=1), base=5, mid="b", payload={"amount": "9"})
    plan = plan_push(_snap(cur), [m1, m2], "d1", NOW)
    assert [r.status for r in plan.results] == ["noop", "applied"]
    assert not plan.results[1].conflict


def test_stale_independent_edit_from_same_device_does_not_overwrite_in_batch():
    """Регрессия на уровне пачки: в записи версия 6 от d1, d1 присылает независимую правку на базе 4
    с более ранним временем (например, восстановленную из резервной копии outbox)."""
    cur = rec(6, NOW, device="d1", mid="x")
    plan = plan_push(_snap(cur), [mut(NOW - dt.timedelta(hours=1), base=4, mid="old")], "d1", NOW)
    assert plan.results[0].status == "rejected" and plan.results[0].record == cur
    assert not plan.upserts


def test_replayed_chain_keeps_original_base_for_next_offline_edit():
    """Регрессия (симуляция, seed 325). Пачка [создание, правка] применена (версии 8, 9), ответ
    потерян. Устройство правит дальше офлайн — база по-прежнему 0. Запись удалили и очистили.
    При повторе пачки цепочка должна помнить исходную базу 0, чтобы новая правка перебазировалась
    на 9 и была отклонена как `deleted`, а не создала удалённую запись заново."""
    logs = {
        "c": (mut(NOW, base=0, mid="c").request_hash("d1"), {"status": "applied", "version": 8, "base": 0}),
        "e": (mut(NOW, base=0, mid="e").request_hash("d1"), {"status": "applied", "version": 9, "base": 8}),
    }
    muts = [mut(NOW, base=0, mid="c"), mut(NOW, base=0, mid="e"), mut(NOW, base=0, mid="new")]
    plan = plan_push(_snap(last=20, logs=logs), muts, "d1", NOW)  # записи нет: tombstone очищен
    assert [r.status for r in plan.results] == ["applied", "applied", "rejected"]
    assert plan.results[2].reason == "deleted" and not plan.upserts
