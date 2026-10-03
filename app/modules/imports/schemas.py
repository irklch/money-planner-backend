import uuid
from datetime import date
from typing import Literal

from pydantic import Field

from app.core.schemas import ApiModel, Money


class BankOut(ApiModel):
    code: str
    name: str


class PeriodOut(ApiModel):
    from_: date = Field(alias="from", serialization_alias="from")
    to: date


class ParsedOperation(ApiModel):
    date: date
    amount: Money
    comment: str | None
    category_id: uuid.UUID | None
    category_status: Literal["assigned", "suggested", "unassigned"]


class PartiallyReadWarning(ApiModel):
    code: Literal["partially_read"] = "partially_read"
    unread_dates: list[date]
    unread_row_count: int


class ParseResult(ApiModel):
    bank: BankOut | None
    period: PeriodOut
    operations: list[ParsedOperation]
    skipped_income_count: int
    warnings: list[PartiallyReadWarning]
