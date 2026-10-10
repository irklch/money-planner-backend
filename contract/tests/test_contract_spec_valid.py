"""Спецификации валидны по OpenAPI 3.1, примеры проходят свои схемы, YAML без дубликатов ключей."""

import pathlib

import contract_spec
import pytest
import yaml
from conftest import CONTRACT


@pytest.mark.parametrize("name", ["openapi.yaml", "openapi-internal.yaml"])
def test_spec_is_valid_openapi_31(name):
    assert contract_spec.check(CONTRACT / name) == []
    assert contract_spec.load_yaml(CONTRACT / name)["openapi"].startswith("3.1.")


def test_duplicate_yaml_keys_are_rejected(tmp_path: pathlib.Path):
    bad = tmp_path / "dup.yaml"
    bad.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(yaml.constructor.ConstructorError):
        contract_spec.load_yaml(bad)


def test_examples_are_checked(spec):
    examples = list(contract_spec.iter_examples(spec))
    # Пример есть у тела запроса и успешного ответа каждого эндпоинта с JSON-телом.
    covered = {label.split(" ")[1] for label, _, _ in examples}
    assert covered >= {
        "/v1/auth/guest",
        "/v1/auth/refresh",
        "/v1/auth/apple",
        "/v1/me",
        "/v1/account",
        "/v1/account/deletions/{deletionId}",
        "/v1/sync/push",
        "/v1/sync/pull",
        "/v1/imports/parse",
        "/v1/imports/{importId}/displayed",
    }


def test_all_component_examples_are_referenced(spec):
    raw = (CONTRACT / "openapi.yaml").read_text(encoding="utf-8")
    unused = [n for n in spec["components"]["examples"] if f"#/components/examples/{n}'" not in raw]
    assert unused == []


def test_invalid_example_is_detected(spec):
    # Самопроверка механизма: заведомо неверный пример ловится валидатором.
    v = contract_spec.schema_validator(spec, "/components/schemas/Timestamp")
    assert list(v.iter_errors("2026-10-11 12:00"))
    assert not list(v.iter_errors("2026-10-11T12:00:00+03:00"))


def test_format_checkers_are_active():
    from jsonschema import FormatChecker

    # date-time без rfc3339-validator молча не проверяется — защищаемся от этого.
    assert {"date", "date-time", "uuid"} <= set(FormatChecker().checkers)
