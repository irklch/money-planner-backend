"""Рандомизированная симуляция: сравнение стратегий и эквивалентность YDB и эталона в памяти."""

from __future__ import annotations

import os

import pytest

from client.simulate import TrackingMemoryStore, compare, run_history

from .conftest import PERM_USERS

# Сколько историй прогонять (по умолчанию 150; для полного прогона — SIM_SEEDS=1000).
SEEDS = int(os.environ.get("SIM_SEEDS", "150"))


# Выбранные стратегии не нарушают ни одного инварианта, а отвергнутые нарушают ожидаемые.
async def test_strategies_invariants():
    table = await compare(range(SEEDS), ["lww_clock", "hlc", "hlc_dw", "version"])
    for name in ("version", "hlc_dw"):
        v = table[name]["violations"]
        assert v.get("divergence", 0) == 0
        assert v.get("stuck_outbox", 0) == 0
        assert v.get("resurrected", 0) == 0
        assert v.get("causal_lost", 0) == 0, f"{name}: causally later edit lost"
    # Ожидаемые дефекты отвергнутых вариантов — доказательство, что проверка их ловит.
    assert table["lww_clock"]["violations"].get("causal_lost", 0) > 0  # без HLC/baseVersion
    assert table["hlc"]["violations"].get("resurrected", 0) > 0  # LWW для удалений (§8.4 документа)
    assert table["hlc"]["violations"].get("causal_lost", 0) == 0


@pytest.mark.ydb
@pytest.mark.parametrize("strategy", ["version", "hlc_dw"])
async def test_ydb_matches_memory_reference(store, strategy):
    """Те же истории поверх YDB дают байт-в-байт то же серверное состояние, что эталон в памяти."""
    for i, seed in enumerate(range(1000, 1004)):
        user = PERM_USERS[i]
        ref = await run_history(seed, strategy, TrackingMemoryStore(), user_id=user, steps=120)
        got = await run_history(seed, strategy, store, user_id=user, steps=120)
        assert not got.violations.get("divergence") and not got.violations.get("causal_lost")
        assert got.final == ref.final, f"seed {seed}: YDB state differs from reference"
        assert len(got.final) > 10
