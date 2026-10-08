"""Модульные тесты правил разрешения конфликтов (без YDB)."""

from __future__ import annotations

import datetime as dt

import pytest

from syncproto import hlc
from syncproto.models import Mutation, StoredRecord
from syncproto.resolve import MAX_FUTURE, STRATEGIES

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)


def rec(version: int, ts: dt.datetime, device: str = "d1", deleted: bool = False, h: str | None = None):
    return StoredRecord(
        "expense",
        "e1",
        version,
        NOW,
        ts,
        ts if deleted else None,
        h or hlc.fmt(hlc.to_ms(ts), 0),
        ts,
        device,
        1,
        None if deleted else {"amount": "1"},
        "m0",
    )


def mut(ts: dt.datetime, base: int = 0, op: str = "upsert", h: str | None = None):
    return Mutation(
        "m1",
        "expense",
        "e1",
        op,
        base,
        h or hlc.fmt(hlc.to_ms(ts), 0),
        NOW,
        ts,
        ts if op == "delete" else None,
        1,
        None if op == "delete" else {"amount": "2"},
    )


V = STRATEGIES["version"]


def test_version_base_match_applies_even_with_clock_far_behind():
    cur = rec(5, NOW)
    d = V.decide(cur, mut(NOW - dt.timedelta(days=365), base=5), "d2", NOW, 5)
    assert d.apply and not d.conflict
    assert d.order_ts == NOW  # монотонная метка не откатывается назад


def test_version_conflict_decided_by_time_then_device():
    cur = rec(5, NOW)
    assert V.decide(cur, mut(NOW + dt.timedelta(seconds=1), base=4), "d2", NOW, 4).apply
    assert not V.decide(cur, mut(NOW - dt.timedelta(seconds=1), base=4), "d2", NOW, 4).apply
    assert V.decide(cur, mut(NOW, base=4), "d2", NOW, 4).apply  # ничья → больший deviceId
    assert not V.decide(rec(5, NOW, device="d3"), mut(NOW, base=4), "d2", NOW, 4).apply


@pytest.mark.parametrize("name", ["version", "hlc_dw", "hlc", "lww_clock"])
def test_same_device_tie_is_applied_same_mutation_is_noop(name):
    """Регрессия из симуляции: часы устройства на сутки вперёд → обе правки обрезаны до now+5 мин
    и получили одинаковую метку; вторая молча становилась noop и терялась."""
    s = STRATEGIES[name]
    far = NOW + dt.timedelta(days=1)
    first = s.decide(None, mut(far), "d1", NOW, 0)
    cur = StoredRecord("expense", "e1", 5, NOW, far, None, first.hlc, first.order_ts, "d1", 1, {"a": 1}, "m0")
    assert s.decide(cur, mut(far + dt.timedelta(seconds=1), base=0), "d1", NOW, 0).apply
    same = StoredRecord(
        "expense", "e1", 5, NOW, far, None, first.hlc, first.order_ts, "d1", 1, {"a": 1}, "m1"
    )
    assert s.decide(same, mut(far), "d1", NOW, 0).status == "noop"


def test_future_clock_is_clamped():
    cur = rec(5, NOW + dt.timedelta(minutes=4))
    d = V.decide(cur, mut(NOW + dt.timedelta(days=1), base=4), "d2", NOW, 4)
    assert d.apply and d.order_ts == NOW + MAX_FUTURE
    assert STRATEGIES["hlc"].decide(None, mut(NOW + dt.timedelta(days=1)), "d2", NOW, 0).hlc == hlc.fmt(
        hlc.to_ms(NOW + MAX_FUTURE), 0
    )


@pytest.mark.parametrize("name", ["version", "hlc_dw"])
def test_delete_wins_and_tombstone_is_final(name):
    s = STRATEGIES[name]
    cur = rec(5, NOW)
    assert s.decide(cur, mut(NOW - dt.timedelta(hours=1), base=3, op="delete"), "d2", NOW, 3).apply
    dead = rec(6, NOW, deleted=True)
    later_edit = s.decide(dead, mut(NOW + dt.timedelta(minutes=1), base=5), "d3", NOW, 5)
    assert not later_edit.apply and later_edit.reason == "deleted"
    assert s.decide(dead, mut(NOW, base=6, op="delete"), "d3", NOW, 6).status == "noop"


def test_doc_hlc_lww_resurrects_deleted_record():
    """architecture-local-first.md §8.4: «удаление против редактирования решается тем же LWW».
    Офлайн-правка с более поздним HLC воскрешает удалённую запись."""
    dead = rec(6, NOW, deleted=True)
    d = STRATEGIES["hlc"].decide(dead, mut(NOW + dt.timedelta(minutes=1), base=5), "d3", NOW, 5)
    assert d.apply  # воскрешение — поэтому в варианте A нужен delete wins


def test_wall_clock_lww_loses_causally_later_edit_hlc_and_version_do_not():
    """Ради этого сценария и существует HLC.
    Телефон (часы верные) правит в 12:00. Планшет с часами на 10 минут позади получает эту правку
    и правит ПОСЛЕ неё (по реальному времени), но его updatedAt = 11:51."""
    phone_ts = NOW
    cur = rec(5, phone_ts, device="phone")
    tablet_wall = NOW - dt.timedelta(minutes=9)  # реально 12:01, часы показывают 11:51
    # Планшет видел HLC телефона → его HLC = max(свой, увиденный) + 1.
    clock = hlc.HybridClock(lambda: hlc.to_ms(tablet_wall))
    clock.observe(cur.hlc)
    edit = mut(tablet_wall, base=5, h=clock.tick())
    assert not STRATEGIES["lww_clock"].decide(cur, edit, "tablet", NOW, 5).apply  # правка потеряна
    assert STRATEGIES["hlc"].decide(cur, edit, "tablet", NOW, 5).apply  # HLC: сохранена
    assert V.decide(cur, edit, "tablet", NOW, 5).apply  # baseVersion: сохранена без часов


def test_hlc_clock_properties():
    t = [1000]
    c = hlc.HybridClock(lambda: t[0])
    a = c.tick()
    b = c.tick()  # часы стоят → растёт счётчик
    assert b > a
    c.observe(hlc.fmt(5000, 7))  # пришло из будущего
    assert c.tick() > hlc.fmt(5000, 7)
    t[0] = 10  # часы ушли назад
    assert c.tick() > hlc.fmt(5000, 8)
