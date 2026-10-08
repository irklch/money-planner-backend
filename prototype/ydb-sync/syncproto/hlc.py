"""Hybrid Logical Clock (Kulkarni et al., 2014) — для варианта A.

Строка `"<physical ms:15>.<counter:6>"` сравнивается лексикографически так же, как пара чисел.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable


def fmt(ms: int, counter: int) -> str:
    return f"{ms:015d}.{counter:06d}"


def parse(value: str) -> tuple[int, int]:
    ms, counter = value.split(".")
    return int(ms), int(counter)


def to_ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def clamp(value: str, cap: dt.datetime) -> str:
    """Сервер не принимает HLC дальше `cap` (now + допуск): иначе одно устройство с часами
    в будущем навсегда выигрывало бы все конфликты и «отравляло» HLC остальных."""
    ms, _ = parse(value)
    cap_ms = to_ms(cap)
    return value if ms <= cap_ms else fmt(cap_ms, 0)


class HybridClock:
    """Клиентские часы. Состояние (`l`, `c`) клиент обязан хранить персистентно."""

    def __init__(self, now_ms: Callable[[], int], l: int = 0, c: int = 0) -> None:  # noqa: E741
        self._now_ms = now_ms
        self.l = l
        self.c = c

    def tick(self) -> str:
        """Локальное событие (изменение записи)."""
        pt = self._now_ms()
        if pt > self.l:
            self.l, self.c = pt, 0
        else:
            self.c += 1
        return fmt(self.l, self.c)

    def observe(self, remote: str) -> None:
        """Получено чужое изменение (pull / ответ push)."""
        rl, rc = parse(remote)
        pt = self._now_ms()
        nl = max(self.l, rl, pt)
        if nl == self.l and nl == rl:
            self.c = max(self.c, rc) + 1
        elif nl == self.l:
            self.c += 1
        elif nl == rl:
            self.c = rc + 1
        else:
            self.c = 0
        self.l = nl
