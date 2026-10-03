from fastapi import APIRouter, Request

from app.core.deps import CurrentUserDep, SessionDep, SettingsDep
from app.core.rate_limit import limiter, parse_gate
from app.modules.imports import service
from app.modules.imports.schemas import ParseResult
from app.modules.imports.upload import read_upload

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post(
    "/parse",
    response_model=ParseResult,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def parse_statement(
    request: Request, user: CurrentUserDep, session: SessionDep, settings: SettingsDep
) -> ParseResult:
    limiter.hit(f"parse:{user.id}", settings.rate_parse_per_minute)
    async with parse_gate.acquire(
        str(user.id), settings.parse_concurrency_per_user, settings.parse_concurrency_global
    ):
        upload = await read_upload(request, "file", settings.max_upload_bytes)
        return await service.parse_statement(session, settings, user.id, upload.filename, upload.data)
