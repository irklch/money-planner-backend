"""Реестр банковских адаптеров.

ВАЖНО: адаптер добавляется сюда только после проверки на реальных анонимизированных
выписках банка (тест с образцом в tests/fixtures/banks/<code>/). Поддерживаются только
проверенные варианты выписок; остальные файлы получают 422 unknown_bank.

| Банк       | code | Формат                                   | Образец                    |
|------------|------|------------------------------------------|----------------------------|
| Альфа-Банк | alfa | .xlsx «Выписка по счету» (рубли)         | tests/fixtures/banks/alfa/ |
"""

from app.modules.imports.parsing.alfa import AlfaBankAdapter
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


register(AlfaBankAdapter())
