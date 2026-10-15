"""Проверка ответов приложения против contract/openapi.yaml тем же `conforms()`, что у эталонной модели."""

import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "contract" / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import contract_spec  # noqa: E402
import refmodel as rm  # noqa: E402

SPEC = contract_spec.load_yaml(ROOT / "contract" / "openapi.yaml")


def conforms(method: str, path: str, r: httpx.Response) -> None:
    """Статус объявлен в контракте, тело проходит схему, код ошибки — из x-error-codes, заголовки есть."""
    body = r.json() if r.content else None
    rm.conforms(SPEC, method, path, rm.Resp(r.status_code, body, r.headers))
    if body and "error" in body:
        assert body["error"]["requestId"] == r.headers["X-Request-Id"]
