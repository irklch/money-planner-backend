import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.dates import TIMEZONE_HEADER, parse_timezone, today_in
from app.core.errors import ApiError
from app.core.security import decode_access_token, utcnow
from app.db.schema import Device
from app.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# devices.last_seen_at обновляется не чаще раза в сутки при любом запросе устройства (план §2.2).
LAST_SEEN_INTERVAL = timedelta(days=1)


@dataclass(frozen=True)
class Subject:
    """Субъект запроса — только из access JWT (контракт §3). E2: гость; аккаунт (`account_id`) — E5."""

    kind: str
    device_id: uuid.UUID


async def touch_device(session: AsyncSession, device_id: uuid.UUID) -> bool:
    """Отметить активность устройства (раз в сутки). False — записи устройства нет."""
    now = utcnow()
    seen = await session.scalar(select(Device.last_seen_at).where(Device.device_id == device_id))
    if seen is None:
        return False
    if seen <= now - LAST_SEEN_INTERVAL:
        await session.execute(
            update(Device)
            .where(Device.device_id == device_id, Device.last_seen_at <= now - LAST_SEEN_INTERVAL)
            .values(last_seen_at=now)
        )
        await session.commit()
    return True


async def get_subject(request: Request, session: SessionDep, settings: SettingsDep) -> Subject:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError("access_token_invalid")
    claims = decode_access_token(settings, token.strip(), utcnow())
    # Устройство удалено (очистка неактивных через 12 мес., E8) — токен больше ничего не открывает.
    if not await touch_device(session, claims.device_id):
        raise ApiError("access_token_invalid")
    return Subject(kind=claims.kind, device_id=claims.device_id)


SubjectDep = Annotated[Subject, Depends(get_subject)]


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID


async def get_current_user(subject: SubjectDep) -> CurrentUser:
    # Только для модулей, отключённых в E1 (не смонтированы; код удаляется в E11): их маршрутов нет.
    return CurrentUser(id=subject.device_id)


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
