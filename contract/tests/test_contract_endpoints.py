"""Состав публичного контракта (план §11, E0), авторизация, заголовки, отделение /internal/*."""

import pytest

# (method, path) → (субъект, этап реализации) — по таблице E0–E8 плана §11.
EXPECTED = {
    ("get", "/healthz"): ("none", "E1"),
    ("post", "/v1/auth/guest"): ("none", "E2"),
    ("post", "/v1/auth/refresh"): ("none", "E2"),
    ("post", "/v1/auth/apple"): ("guest", "E5"),
    ("post", "/v1/auth/logout"): ("none", "E5"),
    ("get", "/v1/me"): ("any", "E4"),
    ("delete", "/v1/account"): ("account", "E7"),
    ("get", "/v1/account/deletions/{deletionId}"): ("guest", "E7"),
    ("post", "/v1/sync/push"): ("account", "E6"),
    ("get", "/v1/sync/pull"): ("account", "E6"),
    ("post", "/v1/imports/parse"): ("any", "E3"),
    ("post", "/v1/imports/{importId}/displayed"): ("any", "E4"),
    ("post", "/v1/apple/notifications"): ("none", "E7"),
}
SECURITY = {
    "none": [],
    "guest": [{"guestBearer": []}],
    "account": [{"accountBearer": []}],
    "any": [{"guestBearer": []}, {"accountBearer": []}],
}
INTERNAL = {
    ("post", "/internal/deletions/run"),
    ("post", "/internal/cleanup/tombstones"),
    ("post", "/internal/cleanup/guest-devices"),
    ("post", "/internal/cleanup/refresh-tokens"),
    ("post", "/internal/cleanup/deleted-accounts"),
}


def test_public_operations_match_plan(operations):
    assert set(operations) == set(EXPECTED)


def test_versioned_prefix(operations):
    for _method, path in operations:
        assert path == "/healthz" or path.startswith("/v1/"), path


def test_no_internal_paths_in_public_contract(spec):
    assert not [p for p in spec["paths"] if p.startswith("/internal")]


def test_internal_contract_is_separate(internal_spec):
    import contract_spec

    ops = contract_spec.operations(internal_spec)
    assert set(ops) == INTERNAL
    assert internal_spec["x-contract"]["publishedInGateway"] is False
    for op in ops.values():
        assert op["security"] == [{"yandexIam": []}]


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_operation_metadata(operations, key):
    op = operations[key]
    subject, stage = EXPECTED[key]
    assert op["operationId"] and op["summary"] and op["tags"]
    assert op["x-subject"] == subject
    assert op["x-stage"] == stage
    assert op["security"] == SECURITY[subject]


def test_operation_ids_unique(operations, internal_spec):
    import contract_spec

    ids = [op["operationId"] for op in operations.values()]
    ids += [op["operationId"] for op in contract_spec.operations(internal_spec).values()]
    assert len(ids) == len(set(ids))


def test_tags_declared(spec, operations):
    declared = {t["name"] for t in spec["tags"]}
    for op in operations.values():
        assert set(op["tags"]) <= declared


def test_security_schemes(spec):
    schemes = spec["components"]["securitySchemes"]
    assert set(schemes) == {"guestBearer", "accountBearer"}
    for s in schemes.values():
        assert (s["type"], s["scheme"], s["bearerFormat"]) == ("http", "bearer", "JWT")
    # Глобальной security нет: у каждой операции она явная.
    assert "security" not in spec


def _param_names(op, resolve):
    return {resolve(p)["name"]: resolve(p) for p in op.get("parameters", [])}


@pytest.mark.parametrize("key", [("post", "/v1/auth/guest"), ("post", "/v1/imports/parse")])
def test_idempotency_key_required(operations, resolve, key):
    params = _param_names(operations[key], resolve)
    p = params["Idempotency-Key"]
    assert p["in"] == "header" and p["required"] is True
    assert p["schema"] == {"type": "string", "format": "uuid"}


def test_idempotency_key_only_where_planned(operations, resolve):
    with_key = {k for k, op in operations.items() if "Idempotency-Key" in _param_names(op, resolve)}
    assert with_key == {("post", "/v1/auth/guest"), ("post", "/v1/imports/parse")}


def test_guest_replay_header(operations, resolve):
    resp = resolve(operations[("post", "/v1/auth/guest")]["responses"]["201"])
    assert "Idempotent-Replayed" in resp["headers"]


def test_pull_pagination_parameters(operations, resolve):
    params = _param_names(operations[("get", "/v1/sync/pull")], resolve)
    assert set(params) == {"cursor", "limit", "horizon"}
    assert all(p["in"] == "query" and not p["required"] for p in params.values())
    assert params["limit"]["schema"]["maximum"] == 500
    assert params["limit"]["schema"]["default"] == 500
    assert params["cursor"]["schema"]["default"] == 0


def test_status_codes_of_success(operations):
    success = {k: sorted(s for s in op["responses"] if s.startswith("2")) for k, op in operations.items()}
    assert success == {
        ("get", "/healthz"): ["200"],
        ("post", "/v1/auth/guest"): ["201"],
        ("post", "/v1/auth/refresh"): ["200"],
        ("post", "/v1/auth/apple"): ["200"],
        ("post", "/v1/auth/logout"): ["204"],
        ("get", "/v1/me"): ["200"],
        ("delete", "/v1/account"): ["202"],
        ("get", "/v1/account/deletions/{deletionId}"): ["200"],
        ("post", "/v1/sync/push"): ["200"],
        ("get", "/v1/sync/pull"): ["200"],
        ("post", "/v1/imports/parse"): ["200"],
        ("post", "/v1/imports/{importId}/displayed"): ["200"],
        ("post", "/v1/apple/notifications"): ["200"],
    }


def test_parse_is_multipart_with_json_parts(operations, resolve):
    body = operations[("post", "/v1/imports/parse")]["requestBody"]
    media = body["content"]["multipart/form-data"]
    form = resolve(media["schema"])
    assert form["required"] == ["file", "categories"]
    assert media["encoding"]["categories"]["contentType"] == "application/json"
    assert media["encoding"]["categoryHints"]["contentType"] == "application/json"


def test_delete_account_body_optional(operations):
    assert operations[("delete", "/v1/account")]["requestBody"]["required"] is False
