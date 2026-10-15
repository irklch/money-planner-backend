from fastapi import APIRouter, Request

from app.core.deps import SessionDep, SettingsDep, SubjectDep
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
    request: Request, subject: SubjectDep, session: SessionDep, settings: SettingsDep
) -> ParseResult:
    # Старый разбор до E3; с E2 — по гостевому токену устройства. Пользовательских категорий у устройства
    # нет, поэтому категоризация — по системным категориям; лимиты — на устройство.
    limiter.hit(f"parse:{subject.device_id}", settings.rate_parse_per_minute)
    async with parse_gate.acquire(
        str(subject.device_id), settings.parse_concurrency_per_user, settings.parse_concurrency_global
    ):
        upload = await read_upload(request, "file", settings.max_upload_bytes)
        return await service.parse_statement(
            session, settings, subject.device_id, upload.filename, upload.data
        )
