"""Общие загрузчики контракта для тестов contract/tests (без БД и без приложения)."""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
# Корень — чтобы `app` брался из исходников, а не из установленного пакета; scripts — для contract_spec.
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import contract_spec as contract_tool  # noqa: E402  scripts/contract_spec.py

CONTRACT = ROOT / "contract"


@pytest.fixture(scope="session")
def spec() -> dict:
    return contract_tool.load_yaml(CONTRACT / "openapi.yaml")


@pytest.fixture(scope="session")
def internal_spec() -> dict:
    return contract_tool.load_yaml(CONTRACT / "openapi-internal.yaml")


@pytest.fixture(scope="session")
def config_params() -> dict:
    return json.loads((CONTRACT / "config-parameters.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def registry() -> dict:
    return json.loads((CONTRACT / "system-categories.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def comment_vectors() -> dict:
    return json.loads((CONTRACT / "vectors" / "comment.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def day_mark_vectors() -> dict:
    return json.loads((CONTRACT / "vectors" / "day-mark-id.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def catalog(spec) -> dict:
    return spec["x-error-catalog"]


@pytest.fixture(scope="session")
def schema_validator(spec):
    """validator(pointer) → jsonschema-валидатор схемы из спецификации (OAS 3.1 = JSON Schema 2020-12)."""
    return lambda pointer: contract_tool.schema_validator(spec, pointer)


@pytest.fixture(scope="session")
def operations(spec) -> dict:
    return contract_tool.operations(spec)


@pytest.fixture(scope="session")
def resolve(spec):
    return lambda node: contract_tool.resolve(spec, node)
