"""Базовые типы парсинга выписок и разбор значений ячеек."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol


class StatementError(Exception):
    """Ошибка парсинга с кодом из 09 — Error Model. Без содержимого строк выписки."""

    def __init__(self, code: str, row: int | None = None):
        super().__init__(code)
        self.code = code
        self.row = row


@dataclass
class Sheet:
    name: str
    rows: list[list[Any]]


@dataclass
class Table:
    sheets: list[Sheet]
    fmt: str | None = None  # "xlsx" | "csv" — адаптер заявляет только проверенные форматы


@dataclass
class RawOperation:
    row: int  # номер строки (1-based) — только для диагностики, без содержимого
    date: date
    amount: Decimal  # < 0 — расход, > 0 — поступление
    description: str | None


@dataclass
class UnreadRow:
    row: int
    date: date | None


@dataclass
class AdapterResult:
    operations: list[RawOperation] = field(default_factory=list)
    unread: list[UnreadRow] = field(default_factory=list)
    # Переводы между собственными счетами: не расход и не поступление, в ответ не попадают.
    own_transfers: list[RawOperation] = field(default_factory=list)
    period: tuple[date, date] | None = None


class BankAdapter(Protocol):
    code: str
    name: str

    def detect(self, table: Table) -> bool: ...

    def parse(self, table: Table) -> AdapterResult: ...


# ---------- Разбор значений ----------

_SPACES = re.compile(r"[\s  ]")
_CURRENCY = re.compile(r"(?i)(₽|руб\.?|rub|р\.)")
_DATE_PATTERNS = (
    (re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})"), lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})"), lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})(?!\d)"), lambda m: date(2000 + int(m[3]), int(m[2]), int(m[1]))),
)


def parse_date_cell(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        for rx, build in _DATE_PATTERNS:
            if m := rx.match(s):
                try:
                    return build(m)
                except ValueError:
                    return None
    return None


def parse_amount_cell(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        try:
            return Decimal(str(v)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    if isinstance(v, str):
        s = _CURRENCY.sub("", _SPACES.sub("", v)).replace("−", "-").replace("–", "-")
        if not s:
            return None
        if s.startswith("+"):
            s = s[1:]
        if "," in s and "." in s:
            s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")
        if not re.fullmatch(r"-?\d+(\.\d+)?", s):
            return None
        try:
            return Decimal(s).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    return None


def cell_text(v: Any) -> str:
    if v is None:
        return ""
    return " ".join(str(v).split())


def norm_header(v: Any) -> str:
    return cell_text(v).casefold().replace("ё", "е")
