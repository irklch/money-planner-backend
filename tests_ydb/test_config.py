"""Конфигурация: значения по умолчанию = контракт (§6.3), переопределение из env, проверки безопасности."""

import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from tests_ydb.conftest import ROOT

CONTRACT_ENV = {
    p["name"]: p["default"]
    for p in json.loads((ROOT / "contract" / "config-parameters.json").read_text(encoding="utf-8"))["env"]
}


@pytest.fixture
def clean_env(monkeypatch):
    for name in [*CONTRACT_ENV, "YDB_ENDPOINT", "YDB_AUTH", "YDB_POOL_SIZE", "YDB_TABLE_PREFIX", "ENV"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_match_contract(clean_env):
    s = Settings(_env_file=None)
    assert {name: getattr(s, name.lower()) for name in CONTRACT_ENV} == CONTRACT_ENV


def test_every_contract_parameter_is_configurable():
    assert {n.lower() for n in CONTRACT_ENV} <= set(Settings.model_fields)


@pytest.mark.parametrize(
    "name,raw,expected",
    [
        ("IMPORT_MAX_FILE_BYTES", "1048576", 1_048_576),
        ("IMPORT_MAX_OPERATIONS", "1000", 1000),
        ("IMPORT_PARSE_TIMEOUT_S", "45", 45),
        ("IMPORT_RATE_PER_MINUTE", "2", 2),
        ("IMPORT_RATE_PER_DAY", "4", 4),
        ("IMPORT_CONCURRENCY_PER_DEVICE", "2", 2),
        ("IMPORT_CONCURRENCY_PER_INSTANCE", "8", 8),
        ("IMPORT_UNCONFIRMED_MAX", "5", 5),
        ("IMPORT_FREE_LIMIT", "10", 10),
        ("ENFORCE_QUOTAS", "true", True),
        ("AI_CATEGORIZATION_ENABLED", "false", False),
        ("AI_MAX_DESCRIPTIONS_PER_IMPORT", "100", 100),
        ("GUEST_CREATE_RATE_PER_IP", "20", 20),
    ],
)
def test_env_overrides(clean_env, name, raw, expected):
    clean_env.setenv(name, raw)
    assert getattr(Settings(_env_file=None), name.lower()) == expected


def test_ydb_settings_from_env(clean_env):
    clean_env.setenv("YDB_ENDPOINT", "grpc://localhost:2136")
    clean_env.setenv("YDB_TABLE_PREFIX", "mp_staging")
    clean_env.setenv("YDB_POOL_SIZE", "1")
    s = Settings(_env_file=None)
    assert (s.ydb_endpoint, s.ydb_table_prefix, s.ydb_pool_size, s.ydb_auth) == (
        "grpc://localhost:2136",
        "mp_staging",
        1,
        "anonymous",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"ydb_pool_size": 3},  # план §3.1: 1–2 сессии
        {"ydb_pool_size": 0},
        {"ydb_table_prefix": "../other"},
        {"ydb_table_prefix": "mp test"},
        {"ydb_endpoint": "grpcs://ydb.serverless.yandexcloud.net:2135", "ydb_auth": "anonymous"},
        {"import_max_file_bytes": 3 * 1024 * 1024},  # больше лимита API Gateway 2,5 МБ
        {"import_parse_timeout_s": 61},  # больше таймаута ревизии
        {"ydb_query_timeout_s": 0},
    ],
)
def test_invalid_values_rejected(clean_env, overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_prod_requires_secure_ydb(clean_env):
    base = dict(
        env="prod",
        jwt_secret="x" * 40,
        token_derivation_secret="y" * 40,
    )
    with pytest.raises(RuntimeError, match="YDB_ENDPOINT"):
        Settings(_env_file=None, **base).check_production_ready()
    s = Settings(_env_file=None, **base, ydb_endpoint="grpc://ydb:2136")
    with pytest.raises(RuntimeError, match="TLS"):
        s.check_production_ready()
    ok = Settings(
        _env_file=None,
        **base,
        ydb_endpoint="grpcs://ydb.serverless.yandexcloud.net:2135",
        ydb_database="/ru-central1/b1g/etn",
        ydb_auth="metadata",
    )
    ok.check_production_ready()
