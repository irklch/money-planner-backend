"""Общие типы DTO: camelCase, деньги — decimal-строка с 2 знаками, даты — YYYY-MM-DD."""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer, WithJsonSchema
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

MAX_AMOUNT = Decimal("999999999.99")
_AMOUNT_RE = re.compile(r"^\d{1,12}(\.\d{1,2})?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CENT = Decimal("0.01")


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        hide_input_in_errors=True,
        from_attributes=True,
        extra="ignore",
    )


def _parse_amount(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        d = v
    elif isinstance(v, str) and _AMOUNT_RE.match(v.strip()):
        d = Decimal(v.strip())
    else:
        raise PydanticCustomError("amount_invalid", "amount must be a decimal string with up to 2 digits")
    if d <= 0:
        raise PydanticCustomError("amount_invalid", "amount must be > 0")
    if d > MAX_AMOUNT:
        raise PydanticCustomError("amount_too_large", "amount too large")
    return d.quantize(CENT)


def _parse_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    if isinstance(v, str) and _DATE_RE.match(v):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    raise PydanticCustomError("date_invalid", "date must be YYYY-MM-DD")


def format_money(d: Decimal) -> str:
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


AmountIn = Annotated[
    Decimal,
    BeforeValidator(_parse_amount),
    WithJsonSchema({"type": "string", "pattern": r"^\d+(\.\d{1,2})?$", "example": "1250.00"}),
]
Money = Annotated[
    Decimal,
    PlainSerializer(format_money, return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+\.\d{2}$", "example": "1250.00"}),
]
DateIn = Annotated[
    date,
    BeforeValidator(_parse_date),
    WithJsonSchema({"type": "string", "format": "date", "example": "2026-09-18"}),
]


def parse_date_param(value: str, code: str = "date_invalid") -> date:
    try:
        return _parse_date(value)
    except PydanticCustomError:
        from app.core.errors import ErrorDetail, validation_error

        raise validation_error(ErrorDetail(code=code)) from None
