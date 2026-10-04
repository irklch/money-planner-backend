import uuid
from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.dates import TIMEZONE_HEADER, parse_timezone, today_in
from app.core.errors import ApiError
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID


async def get_current_user(request: Request, session: SessionDep, settings: SettingsDep) -> CurrentUser:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError("access_token_invalid")
    user_id = decode_access_token(settings, token.strip())
    exists = await session.scalar(select(User.id).where(User.id == user_id))
    if exists is None:
        raise ApiError("user_deleted")
    return CurrentUser(id=user_id)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def parse_idempotency_key(value: str | None) -> uuid.UUID:
    if not value:
        raise ApiError("invalid_request")
    try:
        return uuid.UUID(value)
    except ValueError as e:
        raise ApiError("invalid_request") from e


async def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> uuid.UUID:
    return parse_idempotency_key(idempotency_key)


IdempotencyKeyDep = Annotated[uuid.UUID, Depends(require_idempotency_key)]


def client_ip(request: Request) -> str:
    # За Caddy реальный адрес в X-Forwarded-For (Caddy перезаписывает заголовок).
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def client_today(
    x_timezone: Annotated[
        str | None,
        Header(
            alias=TIMEZONE_HEADER,
            description="IANA-идентификатор часового пояса пользователя, например Europe/Moscow. "
            "По нему сервер определяет «сегодня» для проверки date ≤ сегодня.",
        ),
    ] = None,
) -> date:
    return today_in(parse_timezone(x_timezone))


async def client_today_optional(
    x_timezone: Annotated[
        str | None,
        Header(
            alias=TIMEZONE_HEADER,
            description="IANA-идентификатор часового пояса. Обязателен, если в теле меняется date.",
        ),
    ] = None,
) -> date | None:
    return today_in(parse_timezone(x_timezone)) if x_timezone else None


ClientTodayDep = Annotated[date, Depends(client_today)]
ClientTodayOptionalDep = Annotated[date | None, Depends(client_today_optional)]
