"""Проверка контракта API (E0): contract/openapi.yaml и contract/openapi-internal.yaml.

python scripts/contract_spec.py --check   # CI: спецификации валидны по OpenAPI 3.1, ссылки разрешаются,
                                          # дубликатов ключей YAML нет, все примеры проходят свои схемы

Смысловые проверки контракта (эндпоинты, ошибки, лимиты, векторы) — contract/tests.
"""

import pathlib
import sys
from collections.abc import Iterator
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPECS = (ROOT / "contract" / "openapi.yaml", ROOT / "contract" / "openapi-internal.yaml")
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
_BASE_URI = "urn:money-planner:openapi"


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader, который падает на повторяющихся ключах (обычный молча берёт последний)."""

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)


def resolve(spec: dict[str, Any], node: Any) -> Any:
    """Разрешить локальную ссылку {$ref: '#/...'} (цепочкой), иначе вернуть узел как есть."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"only local refs are supported: {ref}")
        node = spec
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def operations(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(method, path) → operation."""
    return {
        (method, path): op
        for path, item in spec.get("paths", {}).items()
        for method, op in item.items()
        if method in HTTP_METHODS
    }


def schema_validator(spec: dict[str, Any], pointer: str) -> Draft202012Validator:
    """Валидатор JSON Schema 2020-12 для схемы по JSON Pointer внутри спецификации."""
    registry = Registry().with_resource(
        _BASE_URI, Resource.from_contents(spec, default_specification=DRAFT202012)
    )
    return Draft202012Validator(
        {"$ref": f"{_BASE_URI}#{pointer}"}, registry=registry, format_checker=FormatChecker()
    )


def _schema_pointer(spec: dict[str, Any], schema: dict[str, Any], fallback: str) -> str:
    """Pointer схемы медиатипа: по $ref, если он есть, иначе — путь до инлайн-схемы."""
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    return ref[1:] if ref else fallback


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def iter_examples(spec: dict[str, Any]) -> Iterator[tuple[str, str, Any]]:
    """(место, pointer схемы, значение) для каждого примера в request/response медиатипах."""
    for (method, path), op in operations(spec).items():
        base = f"/paths/{_escape(path)}/{method}"
        bodies: list[tuple[str, Any]] = []
        if "requestBody" in op:
            bodies.append((f"{base}/requestBody", resolve(spec, op["requestBody"])))
        for status, resp in op.get("responses", {}).items():
            raw = op["responses"][status]
            where = raw["$ref"][1:] if "$ref" in raw else f"{base}/responses/{status}"
            bodies.append((where, resolve(spec, resp)))
        for where, body in bodies:
            for mtype, media in body.get("content", {}).items():
                if "schema" not in media:
                    continue
                pointer = _schema_pointer(spec, media["schema"], f"{where}/content/{_escape(mtype)}/schema")
                label = f"{method.upper()} {path} {where.rsplit('/', 1)[-1]} {mtype}"
                if "example" in media:
                    yield f"{label} example", pointer, media["example"]
                for name, ex in media.get("examples", {}).items():
                    yield f"{label} examples.{name}", pointer, resolve(spec, ex)["value"]


def check(path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    try:
        spec = load_yaml(path)
    except yaml.YAMLError as e:
        return [f"{path.name}: YAML: {e}"]
    try:
        validate(spec)
    except Exception as e:  # openapi-spec-validator: OpenAPIValidationError и ошибки ссылок
        problems.append(f"{path.name}: OpenAPI 3.1: {e}")
        return problems
    for label, pointer, value in iter_examples(spec):
        for err in schema_validator(spec, pointer).iter_errors(value):
            problems.append(f"{path.name}: {label}: {err.message} at {list(err.absolute_path)}")
    return problems


def main() -> int:
    problems = [p for spec_path in SPECS for p in check(spec_path)]
    for p in problems:
        print(p)
    if problems:
        print(f"Контракт невалиден: {len(problems)} проблем(ы)")
        return 1
    print("Контракт валиден: " + ", ".join(p.relative_to(ROOT).as_posix() for p in SPECS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
