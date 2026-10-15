from fastapi import APIRouter, Request, Response

from app.core.deps import IdempotencyKeyDep, SessionDep, SettingsDep, client_ip
from app.core.rate_limit import limiter
from app.modules.auth import service
from app.modules.auth.schemas import GuestSession, GuestSessionRequest, RefreshRequest

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/guest", status_code=201, response_model=GuestSession)
async def create_guest_session(
    body: GuestSessionRequest,
    request: Request,
    response: Response,
    key: IdempotencyKeyDep,
    session: SessionDep,
    settings: SettingsDep,
) -> GuestSession:
    # GUEST_CREATE_RATE_PER_IP: в минуту на IP, до обращения к БД (повторы с тем же ключом тоже учитываются).
    limiter.hit(f"guest:{client_ip(request)}", settings.guest_create_rate_per_ip)
    result, replayed = await service.create_guest(session, settings, key, body)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return result


@router.post("/refresh", response_model=GuestSession)
async def refresh_session(
    body: RefreshRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> GuestSession:
    limiter.hit(f"refresh:{client_ip(request)}", settings.rate_auth_per_minute * 3)
    return await service.refresh(session, settings, body.refresh_token)
