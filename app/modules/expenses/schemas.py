import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.schemas import AmountIn, ApiModel, DateIn, Money

COMMENT_MAX = 200


def clean_comment(v: object) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise PydanticCustomError("comment_too_long", "comment must be a string")
    v = v.strip()
    if not v:
        return None
    if len(v) > COMMENT_MAX:
        raise PydanticCustomError("comment_too_long", "comment is too long")
    return v


class ExpenseCreate(ApiModel):
    date: DateIn
    amount: AmountIn
    category_id: uuid.UUID
    comment: str | None = None

    @field_validator("comment", mode="before")
    @classmethod
    def _comment(cls, v):
        return clean_comment(v)


class ExpenseUpdate(ApiModel):
    date: DateIn | None = None
    amount: AmountIn | None = None
    category_id: uuid.UUID | None = None
    comment: str | None = None

    @field_validator("comment", mode="before")
    @classmethod
    def _comment(cls, v):
        return clean_comment(v)


class ExpenseOut(ApiModel):
    id: uuid.UUID
    date: date
    amount: Money
    category_id: uuid.UUID
    comment: str | None
    source: Literal["manual", "import"]
    created_at: datetime
    updated_at: datetime


class ExpensePage(ApiModel):
    items: list[ExpenseOut]
    next_cursor: str | None


class ImportExpenseIn(ApiModel):
    date: DateIn
    amount: AmountIn
    category_id: uuid.UUID
    comment: str | None = None

    @field_validator("comment", mode="before")
    @classmethod
    def _comment(cls, v):
        return clean_comment(v)


class ImportRequest(ApiModel):
    expenses: list[ImportExpenseIn] = Field(min_length=1)


class ImportResult(ApiModel):
    imported_count: int
    date_from: date
    date_to: date
