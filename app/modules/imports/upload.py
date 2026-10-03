"""Приём multipart-файла целиком в памяти.

Starlette UploadFile использует SpooledTemporaryFile и при размере > 1 МБ пишет файл на диск.
Выписку нельзя сохранять даже временно, поэтому разбираем multipart сами, в памяти.
"""

from dataclasses import dataclass

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

from app.core.errors import ApiError

MULTIPART_OVERHEAD = 64 * 1024


@dataclass
class UploadedFile:
    filename: str | None
    data: bytes


async def read_upload(request: Request, field: str, max_bytes: int) -> UploadedFile:
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise ApiError("invalid_request")

    declared = request.headers.get("content-length")
    limit = max_bytes + MULTIPART_OVERHEAD
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError("file_too_large")

    parts: list[dict] = []
    current: dict = {}
    header_field = bytearray()
    header_value = bytearray()

    def on_part_begin():
        nonlocal current
        current = {"headers": {}, "data": bytearray()}

    def on_header_field(data, start, end):
        header_field.extend(data[start:end])

    def on_header_value(data, start, end):
        header_value.extend(data[start:end])

    def on_header_end():
        current["headers"][bytes(header_field).lower()] = bytes(header_value)
        header_field.clear()
        header_value.clear()

    def on_part_data(data, start, end):
        current["data"].extend(data[start:end])
        if len(current["data"]) > max_bytes:
            raise ApiError("file_too_large")

    def on_part_end():
        parts.append(current)

    parser = MultipartParser(
        boundary,
        callbacks={
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise ApiError("file_too_large")
            parser.write(chunk)
        parser.finalize()
    except ApiError:
        raise
    except Exception:
        raise ApiError("invalid_request") from None

    for part in parts:
        disp = part["headers"].get(b"content-disposition", b"")
        _, dparams = parse_options_header(disp)
        if dparams.get(b"name") == field.encode():
            raw_name = dparams.get(b"filename")
            filename = raw_name.decode("utf-8", "replace") if raw_name is not None else None
            return UploadedFile(filename=filename, data=bytes(part["data"]))
    raise ApiError("invalid_request")
