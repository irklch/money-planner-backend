from fastapi import APIRouter, Request, Response

from app.core.deps import IdempotencyKeyDep, SessionDep, SettingsDep, client_ip
from app.core.rate_limit import limiter
from app.modules.auth import service
from app.modules.auth.schemas import AnonymousRequest, AuthSession, RefreshRequest

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/anonymous", status_code=201, response_model=AuthSession)
async def create_anonymous(
    body: AnonymousRequest,
    request: Request,
    response: Response,
    key: IdempotencyKeyDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AuthSession:
    limiter.hit(f"auth:{client_ip(request)}", settings.rate_auth_per_minute)
    result, replayed = await service.create_anonymous(session, settings, key)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return result


@router.post("/refresh", response_model=AuthSession)
async def refresh(
    body: RefreshRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> AuthSession:
    limiter.hit(f"refresh:{client_ip(request)}", settings.rate_auth_per_minute * 3)
    return await service.refresh(session, settings, body.refresh_token)
