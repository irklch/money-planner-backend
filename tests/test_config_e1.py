"""Конфигурация E1: параметры плана §6.3 = реестр контракта, переопределение из env, без Lockbox и IAM."""

import json
import pathlib

import pytest
from pydantic import ValidationError

from app.core.config import Settings

REGISTRY = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "contract" / "config-parameters.json").read_text()
)
ENV_PARAMS = {p["name"]: p["default"] for p in REGISTRY["env"]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_PARAMS:
        monkeypatch.delenv(name, raising=False)
    for name in ("LOCKBOX_SECRET_ID", "AI_USE_METADATA_IAM", "AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_every_contract_parameter_has_the_contract_default():
    settings = Settings()
    for name, default in ENV_PARAMS.items():
        assert getattr(settings, name.lower()) == default, name


def test_parameters_are_overridden_from_env(monkeypatch):
    monkeypatch.setenv("IMPORT_RATE_PER_DAY", "7")
    monkeypatch.setenv("ENFORCE_QUOTAS", "true")
    monkeypatch.setenv("AI_CATEGORIZATION_ENABLED", "false")
    monkeypatch.setenv("IMPORT_CONCURRENCY_PER_INSTANCE", "2")
    s = Settings()
    assert (s.import_rate_per_day, s.enforce_quotas, s.ai_categorization_enabled) == (7, True, False)
    assert s.import_concurrency_per_instance == 2


@pytest.mark.parametrize(
    ("name", "value"),
    [("IMPORT_RATE_PER_DAY", "0"), ("IMPORT_MAX_FILE_BYTES", "-1"), ("IMPORT_PARSE_TIMEOUT_S", "1000")],
)
def test_invalid_values_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_lockbox_and_metadata_iam_are_gone(monkeypatch):
    # D24: секреты — .env на сервере; ИИ — по API-ключу. Старые переменные просто игнорируются.
    monkeypatch.setenv("LOCKBOX_SECRET_ID", "e6q-old")
    monkeypatch.setenv("AI_USE_METADATA_IAM", "true")
    s = Settings()
    assert not hasattr(s, "lockbox_secret_id") and not hasattr(s, "ai_use_metadata_iam")
    with pytest.raises(ModuleNotFoundError):
        __import__("app.core.secrets")


def test_production_requires_ai_api_key_when_ai_enabled():
    prod = {
        "env": "prod",
        "jwt_secret": "j" * 48,
        "token_derivation_secret": "t" * 48,
        "ai_enabled": True,
        "ai_folder_id": "b1g",
        "ai_model_categorize": "gpt://b1g/yandexgpt-lite/latest",
        "ai_model_insights": "gpt://b1g/yandexgpt/latest",
    }
    with pytest.raises(RuntimeError, match="AI_API_KEY"):
        Settings(**prod).check_production_ready()
    Settings(**prod, ai_api_key="key").check_production_ready()
    Settings(**{**prod, "ai_enabled": False}).check_production_ready()


def test_healthz_parameters_have_sane_defaults():
    s = Settings()
    assert 0 < s.db_health_timeout_s <= 30 and 1 <= s.db_unavailable_retry_after_s <= 300
