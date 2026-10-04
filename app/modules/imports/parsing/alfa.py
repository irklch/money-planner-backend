"""Альфа-Банк: Excel-выписка «Выписка по счету» (.xlsx).

Проверено на образце tests/fixtures/banks/alfa/. Формат файла:
- шапка: «Выписка по счету», «Номер счета», «Валюта счета», «Клиент», «За период с … по …»,
  итоги банка («Поступления», «Расходы») — служебные строки, операциями не являются;
- блок «Операции по счету», под ним строка заголовков: «Дата операции», «Дата проводки», «Код»,
  «Категория», «Описание», «Сумма в валюте счета», «Статус». Ячейки шапки объединённые,
  поэтому колонки ищутся по тексту заголовка, а не по номеру;
- даты «ДД.ММ.ГГГГ», суммы со знаком и десятичной запятой («-1 234,56»);
- после таблицы — подпись сотрудника АО «АЛЬФА-БАНК» и «Страница N из M».

CSV Альфа-Банка не поддерживается: проверенного образца нет.
Правила первой версии (дата, статусы, переводы, поступления, валюта): docs/banks/alfa.md.
"""

import re
from datetime import date
from typing import Any, ClassVar

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
from app.modules.imports.parsing.tabular import HEADER_SCAN_ROWS, TabularAdapter, _Layout

# Статус проведённой операции. Пустой статус банк ставит у карточных операций, по которым уже есть
# дата проводки: в образце они входят в итог «Расходы», а «Неподтвержденные операции» = 0.
POSTED_STATUS = "выполнен"
RUBLE_CODES = {"rur", "rub", "810", "643"}
# Значение поля шапки стоит в 1–2 колонках правее подписи (подпись в объединённых ячейках).
VALUE_SPAN = 3
OWN_TRANSFER = "внутрибанковский перевод между счетами"
_PERIOD = re.compile(r"за период с (\d{2}\.\d{2}\.\d{4}) по (\d{2}\.\d{2}\.\d{4})")
# Признаки документа: заголовок выписки и подпись банка. Без них таблица с похожими колонками
# Альфа-Банком не считается.
_DOC_TITLE = "выписка по счету"
_TABLE_TITLE = "операции по счету"
_BANK_MARK = "альфа-банк"


class AlfaBankAdapter(TabularAdapter):
    code = "alfa"
    name = "Альфа-Банк"
    formats: ClassVar[tuple[str, ...]] = ("xlsx",)
    date_headers = ("дата операции",)
    amount_headers = ("сумма в валюте счета",)
    description_headers = ("описание",)
    status_headers = ("статус",)
    # Все колонки ниже обязательны для распознавания формата.
    required_headers: ClassVar[dict[str, tuple[str, ...]]] = {
        "posting_date": ("дата проводки",),
        "code": ("код",),
        "category": ("категория",),
        "description": description_headers,
        "status": status_headers,
    }

    def _layout(self, table: Table) -> _Layout | None:
        if table.fmt is not None and table.fmt not in self.formats:
            return None
        for sheet in table.sheets:
            if not _has_bank_mark(sheet):
                continue
            head = sheet.rows[:HEADER_SCAN_ROWS]
            for i, row in enumerate(head):
                d, a = self._find(row, self.date_headers), self._find(row, self.amount_headers)
                if d is None or a is None:
                    continue
                cols = {"date": d, "amount": a}
                for key, variants in self.required_headers.items():
                    if (x := self._find(row, variants)) is None:
                        break
                    cols[key] = x
                else:
                    above = " ".join(norm_header(c) for r in head[:i] for c in r)
                    if _DOC_TITLE in above and _TABLE_TITLE in above and _is_ruble(head[:i]):
                        return _Layout(sheet=sheet, header_row=i, cols=cols)
        return None

    def parse(self, table: Table) -> AdapterResult:
        layout = self._layout(table)
        assert layout is not None
        cols, rows = layout.cols, layout.sheet.rows
        header = [norm_header(c) for c in rows[layout.header_row]]
        client = _header_value(rows[: layout.header_row], "клиент")
        result = AdapterResult()

        for idx in range(layout.header_row + 1, len(rows)):
            row = rows[idx]

            def cell(key: str, row: list = row) -> Any:
                i = cols[key]
                return row[i] if i < len(row) else None

            if [norm_header(c) for c in row] == header:
                continue  # заголовок, повторённый на следующей странице
            op_date = parse_date_cell(cell("date"))
            posted_on = parse_date_cell(cell("posting_date"))
            raw_amount = cell_text(cell("amount"))
            if op_date is None and posted_on is None and not raw_amount and not cell_text(cell("code")):
                continue  # пустая или служебная строка: подпись, «Страница N из M»

            amount = parse_amount_cell(cell("amount"))
            status = norm_header(cell("status"))
            posted = status == POSTED_STATUS or (status == "" and posted_on is not None)
            if op_date is None or amount is None or not posted:
                # Неизвестный статус, битая дата или сумма — не операция, а предупреждение partially_read.
                result.unread.append(UnreadRow(row=idx + 1, date=op_date or posted_on))
                continue

            desc = cell_text(cell("description")) or None
            op = RawOperation(row=idx + 1, date=op_date, amount=amount, description=desc)
            if _is_own_transfer(desc, client):
                result.own_transfers.append(op)
            else:
                result.operations.append(op)

        result.period = _period(rows[: layout.header_row], result)
        return result


def _has_bank_mark(sheet: Sheet) -> bool:
    return any(_BANK_MARK in norm_header(c) for r in sheet.rows for c in r if c)


def _header_value(rows: list[list], label: str) -> str | None:
    """Значение поля шапки: первая непустая ячейка в пределах VALUE_SPAN колонок правее подписи.

    Дальше в той же строке начинается правый блок шапки («Расходы», «Поступления»), и пустое поле
    не должно подхватывать его текст. Пустое или отсутствующее поле → None.
    """
    for row in rows:
        for i, c in enumerate(row):
            if norm_header(c) == label:
                rest = [cell_text(v) for v in row[i + 1 : i + 1 + VALUE_SPAN] if cell_text(v)]
                return rest[0] if rest else None
    return None


def _is_ruble(rows: list[list]) -> bool:
    """Только явно указанная рублёвая валюта. Нет поля, пустое или другая валюта → не распознаём."""
    currency = _header_value(rows, "валюта счета")
    return currency is not None and currency.casefold() in RUBLE_CODES


def _is_own_transfer(desc: str | None, client: str | None) -> bool:
    """«Внутрибанковский перевод между счетами, <ФИО клиента>. Со счёта … на счёт …».

    Свой перевод — только при подтверждённом совпадении ФИО с «Клиент» из шапки. Без клиента
    в шапке перевод остаётся обычной операцией (списание — расход на проверку, поступление — доход).
    """
    text = norm_header(desc)
    if not text.startswith(OWN_TRANSFER):
        return False
    if not client:
        return False
    owner = text[len(OWN_TRANSFER) :].lstrip(" ,").split(".", 1)[0].strip()
    return owner == norm_header(client)


def _period(rows: list[list], result: AdapterResult) -> tuple[date, date] | None:
    dates = [op.date for op in (*result.operations, *result.own_transfers)]
    for row in rows:
        for c in row:
            if m := _PERIOD.search(norm_header(c)):
                start, end = parse_date_cell(m[1]), parse_date_cell(m[2])
                if start and end and start <= end:
                    # Период шапки, расширенный датами операций, если они за него выходят.
                    return min([start, *dates]), max([end, *dates])
    return (min(dates), max(dates)) if dates else None
