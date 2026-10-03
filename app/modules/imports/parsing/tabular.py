"""Основа для адаптеров табличных выписок: поиск строки заголовков и маппинг колонок.

Конкретный банк описывается декларативно (заголовки колонок) и регистрируется в registry
только после проверки на реальных анонимизированных образцах.
"""

from dataclasses import dataclass
from typing import ClassVar

from app.modules.imports.parsing.base import (
    AdapterResult,
    RawOperation,
    Sheet,
    Table,
    UnreadRow,
    cell_text,
    norm_header,
    parse_amount_cell,
    parse_date_cell,
)

HEADER_SCAN_ROWS = 40


@dataclass
class _Layout:
    sheet: Sheet
    header_row: int
    cols: dict[str, int]


class TabularAdapter:
    code: ClassVar[str]
    name: ClassVar[str]
    # Варианты заголовков (нормализованные: casefold, ё→е). Обязательные: date, amount.
    date_headers: ClassVar[tuple[str, ...]]
    amount_headers: ClassVar[tuple[str, ...]]
    description_headers: ClassVar[tuple[str, ...]] = ()
    # Опционально: статус операции и значения, которые означают «не проведена/отклонена».
    status_headers: ClassVar[tuple[str, ...]] = ()
    skip_statuses: ClassVar[tuple[str, ...]] = ()
    # Знак суммы: True — расходы в выписке отрицательные; False — положительные (инвертируем).
    expenses_negative: ClassVar[bool] = True
    # Дополнительный признак банка: строки, одна из которых должна встретиться в шапке файла.
    signature: ClassVar[tuple[str, ...]] = ()

    def _find(self, row: list, variants: tuple[str, ...]) -> int | None:
        normalized = [norm_header(c) for c in row]
        for v in variants:
            if v in normalized:
                return normalized.index(v)
        return None

    def _layout(self, table: Table) -> _Layout | None:
        for sheet in table.sheets:
            head = sheet.rows[:HEADER_SCAN_ROWS]
            if self.signature:
                blob = " ".join(norm_header(c) for r in head for c in r)
                if not any(s in blob for s in self.signature):
                    continue
            for i, row in enumerate(head):
                d, a = self._find(row, self.date_headers), self._find(row, self.amount_headers)
                if d is None or a is None:
                    continue
                cols = {"date": d, "amount": a}
                if (x := self._find(row, self.description_headers)) is not None:
                    cols["description"] = x
                if (x := self._find(row, self.status_headers)) is not None:
                    cols["status"] = x
                return _Layout(sheet=sheet, header_row=i, cols=cols)
        return None

    def detect(self, table: Table) -> bool:
        return self._layout(table) is not None

    def parse(self, table: Table) -> AdapterResult:
        layout = self._layout(table)
        assert layout is not None
        result = AdapterResult()
        cols = layout.cols
        skip = {norm_header(s) for s in self.skip_statuses}
        for idx in range(layout.header_row + 1, len(layout.sheet.rows)):
            row = layout.sheet.rows[idx]
            if not any(cell_text(c) for c in row):
                continue

            def cell(key: str, row: list = row):
                i = cols.get(key)
                return row[i] if i is not None and i < len(row) else None

            if "status" in cols and norm_header(cell("status")) in skip:
                continue
            d = parse_date_cell(cell("date"))
            amount = parse_amount_cell(cell("amount"))
            if d is None or amount is None:
                result.unread.append(UnreadRow(row=idx + 1, date=d))
                continue
            if not self.expenses_negative:
                amount = -amount
            desc = cell_text(cell("description")) or None
            result.operations.append(RawOperation(row=idx + 1, date=d, amount=amount, description=desc))
        return result
