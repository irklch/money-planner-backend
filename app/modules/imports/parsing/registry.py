"""Реестр банковских адаптеров.

ВАЖНО: адаптер добавляется сюда только после проверки на реальных анонимизированных
выписках банка (тест с образцом в tests/fixtures/banks/<code>/). Пока проверенных образцов нет,
реестр пуст и любой файл получает 422 unknown_bank. Ориентир из UX: Т-Банк, Сбер, Альфа-Банк.
"""

from app.modules.imports.parsing.base import BankAdapter, Table

_ADAPTERS: list[BankAdapter] = []


def register(adapter: BankAdapter) -> None:
    if any(a.code == adapter.code for a in _ADAPTERS):
        raise ValueError(f"adapter {adapter.code} already registered")
    _ADAPTERS.append(adapter)


def unregister(code: str) -> None:
    _ADAPTERS[:] = [a for a in _ADAPTERS if a.code != code]


def adapters() -> list[BankAdapter]:
    return list(_ADAPTERS)


def detect(table: Table) -> BankAdapter | None:
    for adapter in _ADAPTERS:
        if adapter.detect(table):
            return adapter
    return None
