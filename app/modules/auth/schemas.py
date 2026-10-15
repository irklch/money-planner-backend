"""DTO авторизации по контракту (contract/openapi.yaml): GuestSessionRequest, RefreshRequest, GuestSession."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import ApiModel


class GuestSessionRequest(ApiModel):
    # deviceId клиент не передаёт: id устройства создаёт сервер (лишние поля игнорируются).
    platform: Literal["ios", "android"]
    app_version: str = Field(min_length=1, max_length=32)


class RefreshRequest(ApiModel):
    refresh_token: str = Field(min_length=16, max_length=256)


class GuestSession(ApiModel):
    subject_kind: Literal["guest"] = "guest"
    device_id: uuid.UUID
    access_token: str
    access_token_expires_at: datetime
    refresh_token: str
    refresh_token_expires_at: datetime
