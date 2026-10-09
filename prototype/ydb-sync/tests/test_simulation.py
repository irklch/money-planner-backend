"""Рандомизированная симуляция варианта B и эквивалентность YDB и эталона в памяти."""

from __future__ import annotations

import os

import pytest

from client.simulate import TrackingMemoryStore, run_history, run_many

from .conftest import PERM_USERS

# Сколько историй прогонять (по умолчанию 150; для полного прогона — SIM_SEEDS=1000).
SEEDS = int(os.environ.get("SIM_SEEDS", "150"))


# Ни одного нарушения инвариантов на SEEDS историях; resync и очистка tombstones реально случались.
async def test_invariants_hold():
    table = await run_many(range(SEEDS))
    v = table["violations"]
    for name in ("divergence", "stuck_outbox", "resurrected", "unarchived", "causal_lost"):
        assert v.get(name, 0) == 0, f"{name}: {table}"
    assert table["purges"] > 0 and table["resyncs"] > 0, "симуляция не проверила resync"


@pytest.mark.ydb
async def test_ydb_matches_memory_reference(store):
    """Те же истории поверх YDB дают байт-в-байт то же серверное состояние, что эталон в памяти."""
    for i, seed in enumerate(range(1000, 1004)):
        user = PERM_USERS[i]
        ref = await run_history(seed, TrackingMemoryStore(), user_id=user, steps=120)
        got = await run_history(seed, store, user_id=user, steps=120)
        assert not got.violations.get("divergence") and not got.violations.get("causal_lost")
        assert got.final == ref.final, f"seed {seed}: YDB state differs from reference"
        assert len(got.final) > 10
