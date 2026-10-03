import uuid
from datetime import datetime
from typing import Literal

import emoji as emoji_lib
from pydantic import field_validator
from pydantic_core import PydanticCustomError

from app.core.schemas import ApiModel

NAME_MAX = 24


def clean_name(v: object) -> str:
    if not isinstance(v, str) or not v.strip():
        raise PydanticCustomError("name_required", "name is required")
    v = " ".join(v.split())
    if len(v) > NAME_MAX:
        raise PydanticCustomError("name_too_long", "name is too long")
    return v


def clean_emoji(v: object) -> str:
    if not isinstance(v, str) or not v.strip():
        raise PydanticCustomError("emoji_required", "emoji is required")
    v = v.strip()
    if not emoji_lib.is_emoji(v):  # ровно один emoji (одна графема), без текста вокруг
        raise PydanticCustomError("emoji_invalid", "exactly one emoji is required")
    return v


class CategoryCreate(ApiModel):
    name: str
    emoji: str

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return clean_name(v)

    @field_validator("emoji", mode="before")
    @classmethod
    def _emoji(cls, v):
        return clean_emoji(v)


class CategoryUpdate(ApiModel):
    name: str | None = None
    emoji: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return None if v is None else clean_name(v)

    @field_validator("emoji", mode="before")
    @classmethod
    def _emoji(cls, v):
        return None if v is None else clean_emoji(v)


class CategoryOut(ApiModel):
    id: uuid.UUID
    name: str
    emoji: str
    kind: Literal["system", "custom"]
    color_index: int
    sort_order: int | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CategoryList(ApiModel):
    items: list[CategoryOut]
