"""Гостевая запись устройства и сессии (план §2.2, §2.7, §2.8; контракт: /v1/auth/guest, /v1/auth/refresh).

- `POST /auth/guest` создаёт `devices` и первую строку `refresh_tokens` (новая family) в одной транзакции.
  id устройства создаёт сервер: клиент не может указать чужое устройство, доступ к нему — только через
  его refresh-токен.
- Первый refresh-токен = HMAC(секрет, Idempotency-Key). Повтор с тем же ключом в течение 24 ч, пока этот
  токен не ротирован, не отозван и не истёк, и с тем же телом возвращает тот же `deviceId` и тот же
  refresh-токен (вычислением; в БД только SHA-256) и новый access JWT. Иначе — `409
  idempotency_key_reused` без токенов. Параллельные повторы сериализуются advisory-блокировкой по ключу.
- Refresh: ротация в той же family; новый токен = HMAC(секрет, старый токен). Повтор старого токена в
  grace-окне 60 с возвращает ту же пару (тот же refresh-токен и тот же access JWT), если новый токен
  ещё не использован и не отозван. Иначе — reuse: вся family отзывается (`reuse_detected`) → `401
  refresh_token_invalid`. Все операции над family сериализуются advisory-блокировкой по family_id,
  поэтому отзыв и параллельная ротация не оставляют действующих токенов в отозванной family.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import touch_device
from app.core.errors import ApiError
from app.core.security import (
    create_access_token,
    derive_jti,
    derive_refresh_token,
    hash_refresh_token,
    refresh_expires_at,
    utcnow,
)
from app.db.schema import Device, RefreshToken
from app.modules.auth.schemas import GuestSession, GuestSessionRequest

GUEST = "guest"
GUEST_KEY = "guest-key"  # назначение HMAC первого токена (не совпадает со старым "anon")
ROTATE = "rotate"
# Причины отзыва, которые клиент различает (§2.8); остальные (reuse_detected, logout) → refresh_token_invalid.
REVOKED_CODES = {"session_revoked": "session_revoked", "account_deleted": "account_deleted"}


def _lock_id(value: str) -> int:
    return int.from_bytes(uuid.uuid5(uuid.NAMESPACE_OID, value).bytes[:8], "big", signed=True)


async def _lock(session: AsyncSession, value: str) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(_lock_id(value))))


def _session(
    settings: Settings, token: str, row: RefreshToken, issued_at: datetime, jti: str
) -> GuestSession:
    access, access_exp = create_access_token(settings, GUEST, row.device_id, issued_at, jti)
    return GuestSession(
        device_id=row.device_id,
        access_token=access,
        access_token_expires_at=access_exp,
        refresh_token=token,
        refresh_token_expires_at=row.expires_at,
    )


def _issued_session(settings: Settings, token: str, row: RefreshToken) -> GuestSession:
    """Пара, выданная вместе со строкой refresh-токена: одинакова при каждом вызове."""
    return _session(settings, token, row, row.created_at, derive_jti(settings, row.token_hash))


def _new_token_row(
    settings: Settings, token: str, family_id: uuid.UUID, device_id: uuid.UUID, now: datetime
) -> RefreshToken:
    return RefreshToken(
        token_hash=hash_refresh_token(token),
        family_id=family_id,
        subject_kind=GUEST,
        device_id=device_id,
        user_id=None,
        created_at=now,
        expires_at=refresh_expires_at(settings, now),
    )


async def _locked_row(session: AsyncSession, token_hash: str) -> RefreshToken | None:
    """Строка токена под блокировкой его family; перечитывается после блокировки."""
    row = await session.get(RefreshToken, token_hash)
    if row is None:
        return None
    await _lock(session, f"family:{row.family_id}")
    return await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def create_guest(
    session: AsyncSession, settings: Settings, key: uuid.UUID, body: GuestSessionRequest
) -> tuple[GuestSession, bool]:
    """Возвращает (сессия, replayed)."""
    now = utcnow()
    token = derive_refresh_token(settings, GUEST_KEY, str(key))
    token_hash = hash_refresh_token(token)
    await _lock(session, f"guest:{token_hash}")

    row = await _locked_row(session, token_hash)
    if row is not None:
        device = await session.get(Device, row.device_id)
        window = timedelta(hours=settings.guest_idempotency_window_hours)
        # Токен отдаётся повторно, только пока он единственный действующий токен этой записи и ключ
        # повторён с тем же запросом. Использованный, отозванный или истёкший токен не возвращается.
        if (
            row.rotated_at is not None
            or row.revoked_at is not None
            or row.expires_at <= now
            or row.created_at < now - window
            or device is None
            or (device.platform, device.app_version) != (body.platform, body.app_version)
        ):
            await session.rollback()
            raise ApiError("idempotency_key_reused")
        await session.commit()
        # Тот же refresh-токен, access JWT — новый (контракт).
        return _session(settings, token, row, now, uuid.uuid4().hex), True

    device = Device(
        device_id=uuid.uuid4(),
        platform=body.platform,
        app_version=body.app_version,
        created_at=now,
        last_seen_at=now,
    )
    session.add(device)
    await session.flush()
    row = _new_token_row(settings, token, uuid.uuid4(), device.device_id, now)
    session.add(row)
    await session.commit()
    return _issued_session(settings, token, row), False


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID, reason: str, now: datetime) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    await session.commit()


async def refresh(session: AsyncSession, settings: Settings, token: str) -> GuestSession:
    now = utcnow()
    row = await _locked_row(session, hash_refresh_token(token))
    # Неизвестный и истёкший токен — refresh_token_invalid; family при этом не трогается.
    # E2 выдаёт только гостевые токены; токены аккаунта (users.state, account_deleted) — E5/E7.
    if row is None or row.expires_at <= now or row.subject_kind != GUEST:
        await session.rollback()
        raise ApiError("refresh_token_invalid")
    if row.revoked_at is not None:
        error = REVOKED_CODES.get(row.revoked_reason or "", "refresh_token_invalid")
        await session.rollback()
        raise ApiError(error)

    child_token = derive_refresh_token(settings, ROTATE, token)
    child_hash = hash_refresh_token(child_token)

    if row.rotated_at is not None:
        if now - row.rotated_at <= timedelta(seconds=settings.refresh_grace_seconds):
            child = await session.get(RefreshToken, child_hash)
            # Потерянный ответ: клиент новый токен не получил, значит и не использовал.
            if child is not None and child.rotated_at is None and child.revoked_at is None:
                await session.commit()
                return _issued_session(settings, child_token, child)
        await _revoke_family(session, row.family_id, "reuse_detected", now)
        raise ApiError("refresh_token_invalid")

    row.rotated_at = now
    child = _new_token_row(settings, child_token, row.family_id, row.device_id, now)
    session.add(child)
    await session.flush()
    await touch_device(session, row.device_id)  # фиксирует транзакцию, если отметка устарела
    await session.commit()
    return _issued_session(settings, child_token, child)
