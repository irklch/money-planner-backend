"""Лимиты в спецификации совпадают со списком параметров конфигурации (план §6.3) и протокола."""

import pytest


def _env(config_params) -> dict:
    return {p["name"]: p for p in config_params["env"]}


def _protocol(config_params) -> dict:
    return {p["name"]: p["value"] for p in config_params["protocol"]}


# Стартовые значения, утверждённые в плане §6.3 [У].
PLAN_DEFAULTS = {
    "IMPORT_MAX_FILE_BYTES": 2_097_152,
    "IMPORT_MAX_OPERATIONS": 5000,
    "IMPORT_PARSE_TIMEOUT_S": 30,
    "IMPORT_RATE_PER_MINUTE": 5,
    "IMPORT_RATE_PER_DAY": 10,
    "IMPORT_CONCURRENCY_PER_DEVICE": 1,
    "IMPORT_CONCURRENCY_PER_INSTANCE": 4,
    "IMPORT_UNCONFIRMED_MAX": 3,
    "IMPORT_FREE_LIMIT": 3,
    "ENFORCE_QUOTAS": False,
    "AI_CATEGORIZATION_ENABLED": True,
    "AI_MAX_DESCRIPTIONS_PER_IMPORT": 500,
    "GUEST_CREATE_RATE_PER_IP": 10,
}
# Какой эндпоинт отвечает ошибкой превышения параметра.
OPERATION_OF = {
    "IMPORT_": ("post", "/v1/imports/parse"),
    "GUEST_CREATE": ("post", "/v1/auth/guest"),
    "SYNC_PUSH": ("post", "/v1/sync/push"),
}


def test_env_defaults_match_plan(config_params):
    env = _env(config_params)
    assert {k: v["default"] for k, v in env.items()} == PLAN_DEFAULTS


def test_env_names_are_unique(config_params):
    names = [p["name"] for p in config_params["env"] + config_params["protocol"] + config_params["client"]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("section", ["env", "protocol"])
def test_exceed_codes_are_declared(config_params, catalog, operations, resolve, section):
    for p in config_params[section]:
        exceed = p.get("onExceed")
        if not exceed:
            continue
        assert catalog[exceed["code"]]["status"] == exceed["status"], p["name"]
        if exceed.get("retryAfter"):
            assert catalog[exceed["code"]].get("retryAfter"), p["name"]
        key = next(op for prefix, op in OPERATION_OF.items() if p["name"].startswith(prefix))
        resp = resolve(operations[key]["responses"][str(exceed["status"])])
        assert exceed["code"] in resp["x-error-codes"], (p["name"], key)


def test_schema_limits_match_protocol(spec, config_params, resolve):
    proto = _protocol(config_params)
    s = spec["components"]["schemas"]
    params = spec["components"]["parameters"]
    assert s["PushRequest"]["properties"]["mutations"]["maxItems"] == proto["SYNC_PUSH_MAX_MUTATIONS"]
    assert params["Limit"]["schema"]["maximum"] == proto["SYNC_PULL_MAX_LIMIT"]
    assert s["PullResponse"]["properties"]["records"]["maxItems"] == proto["SYNC_PULL_MAX_LIMIT"]
    assert s["ExpensePayload"]["properties"]["comment"]["maxLength"] == proto["EXPENSE_COMMENT_MAX_SCALARS"]
    assert s["ParsedOperation"]["properties"]["comment"]["maxLength"] == proto["EXPENSE_COMMENT_MAX_SCALARS"]
    assert s["UserCategoryPayload"]["properties"]["name"]["maxLength"] == proto["CATEGORY_NAME_MAX_SCALARS"]
    assert s["CategoryRef"]["properties"]["name"]["maxLength"] == proto["CATEGORY_NAME_MAX_SCALARS"]
    assert (
        s["UserCategoryPayload"]["properties"]["colorIndex"]["maximum"] == proto["CATEGORY_COLOR_INDEX_MAX"]
    )
    form = s["ParseStatementForm"]["properties"]
    assert form["categoryHints"]["maxItems"] == proto["IMPORT_CATEGORY_HINTS_MAX"]
    assert (
        resolve(form["categoryHints"]["items"])["properties"]["merchantKey"]["maxLength"]
        == (proto["IMPORT_MERCHANT_KEY_MAX_SCALARS"])
    )
    assert proto["SYNC_KNOWN_SCHEMA_VERSIONS"] == [1]
    assert proto["SYNC_PUSH_MAX_BODY_BYTES"] == 1_048_576


def test_file_limit_matches_env(spec, config_params):
    form = spec["components"]["schemas"]["ParseStatementForm"]["properties"]
    assert form["file"]["x-max-bytes-default"] == _env(config_params)["IMPORT_MAX_FILE_BYTES"]["default"]
    # API Gateway принимает до 2,5 МБ с заголовками: файл с запасом под multipart и JSON-части.
    assert form["file"]["x-max-bytes-default"] < 2.5 * 1024 * 1024


def test_money_bounds(schema_validator, config_params):
    v = schema_validator("/components/schemas/Money")
    assert not list(v.iter_errors(_protocol(config_params)["EXPENSE_AMOUNT_MAX"]))
    assert list(v.iter_errors("1000000000.00"))


def test_client_retry_policy(config_params):
    # §8.1: ≤ 4 повторов (5 попыток), бюджет 45 с, потолок задержки 16 с; фоновые — потолок 15 мин.
    client = {p["name"]: p["value"] for p in config_params["client"]}
    assert client == {
        "retry_max_attempts": 5,
        "retry_budget_s": 45,
        "retry_cap_s": 16,
        "background_retry_cap_s": 900,
    }
