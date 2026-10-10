"""Единый формат ошибок, каталог кодов, коды 401 (§2.8), Retry-After и коды по эндпоинтам."""

import pytest

# Коды, которые план (E0, §2.8, §6) требует зафиксировать, и их HTTP-статусы.
PLAN_CODES = {
    "token_expired": 401,
    "refresh_token_invalid": 401,
    "session_revoked": 401,
    "account_deleted": 401,
    "account_required": 403,
    "resync_required": 410,
    "upgrade_required": 426,
    "delivery_expired": 410,
    "idempotency_key_reused": 409,
    "idempotency_in_progress": 409,
    "unconfirmed_imports_limit": 429,
    "file_too_large": 413,
    "statement_too_large": 422,
    "processing_timeout": 503,
    "rate_limited": 429,
}
UNAUTHORIZED_GUEST = {"token_expired", "access_token_invalid"}
UNAUTHORIZED_ACCOUNT = UNAUTHORIZED_GUEST | {"session_revoked", "account_deleted"}

# Коды, которые обязаны быть у конкретного эндпоинта (план §2, §3, §6).
REQUIRED_BY_OPERATION = {
    ("post", "/v1/auth/guest"): {"invalid_request", "idempotency_key_reused", "rate_limited"},
    ("post", "/v1/auth/refresh"): {"refresh_token_invalid", "session_revoked", "account_deleted"},
    ("post", "/v1/auth/apple"): {"apple_token_invalid"},
    ("get", "/v1/account/deletions/{deletionId}"): {"deletion_not_found"},
    ("delete", "/v1/account"): {"account_required"},
    ("post", "/v1/sync/push"): {
        "account_required",
        "upgrade_required",
        "payload_too_large",
        "validation_failed",
    },
    ("get", "/v1/sync/pull"): {"account_required", "resync_required"},
    ("post", "/v1/imports/parse"): {
        "invalid_request",
        "idempotency_key_reused",
        "idempotency_in_progress",
        "file_too_large",
        "unsupported_format",
        "corrupted_file",
        "unknown_bank",
        "empty_statement",
        "income_only",
        "statement_too_large",
        "rate_limited",
        "unconfirmed_imports_limit",
        "processing_failed",
        "processing_timeout",
        "service_unavailable",
    },
    ("post", "/v1/imports/{importId}/displayed"): {"import_not_found", "delivery_expired"},
}


def _codes_by_status(op, resolve) -> dict[str, set[str]]:
    out = {}
    for status, resp in op["responses"].items():
        r = resolve(resp)
        out[status] = set(r.get("x-error-codes", []))
    return out


def test_catalog_matches_error_code_enum(spec, catalog):
    enum = spec["components"]["schemas"]["ErrorCode"]["enum"]
    assert len(enum) == len(set(enum))
    assert set(enum) == set(catalog)


def test_catalog_entries_are_well_formed(catalog):
    for code, entry in catalog.items():
        assert code.islower() and " " not in code
        assert set(entry) <= {"status", "retryable", "retryAfter", "description"}
        assert 400 <= entry["status"] <= 599
        assert isinstance(entry["retryable"], bool) and entry["description"]
        if entry.get("retryAfter"):
            assert entry["retryable"] is True, code


@pytest.mark.parametrize("code,status", sorted(PLAN_CODES.items()))
def test_plan_codes_present(catalog, code, status):
    assert catalog[code]["status"] == status


def test_401_codes_follow_section_2_8(catalog):
    assert {c for c, e in catalog.items() if e["status"] == 401} == UNAUTHORIZED_ACCOUNT | {
        "refresh_token_invalid"
    }
    # Повторяемый только истёкший access JWT (после /auth/refresh); остальные ведут к смене состояния.
    assert {c for c, e in catalog.items() if e["status"] == 401 and e["retryable"]} == {"token_expired"}


def test_retryable_follows_client_retry_policy(catalog):
    # §8.1: автоматически повторяются 5xx, 503 и 429; 4xx кроме 429 — нет (409 in_progress — с Retry-After).
    for code, e in catalog.items():
        if e["status"] >= 500 or e["status"] == 429:
            assert e["retryable"], code
            if e["status"] in (429, 503):
                assert e.get("retryAfter"), code
        elif code not in ("idempotency_in_progress", "token_expired"):
            assert not e["retryable"], code


def test_every_response_declares_request_id(operations, resolve):
    for key, op in operations.items():
        for status, resp in op["responses"].items():
            assert "X-Request-Id" in resolve(resp).get("headers", {}), (key, status)


def test_error_responses_use_envelope_and_catalog(operations, resolve, catalog):
    for key, op in operations.items():
        for status, codes in _codes_by_status(op, resolve).items():
            resp = resolve(op["responses"][status])
            if status.startswith(("4", "5")):
                assert codes, (key, status, "x-error-codes пуст")
                schema = resp["content"]["application/json"]["schema"]
                assert schema == {"$ref": "#/components/schemas/ErrorEnvelope"}, (key, status)
            for code in codes:
                assert code in catalog, (key, code)
                assert str(catalog[code]["status"]) == status, (key, status, code)


def test_every_operation_declares_5xx(operations):
    for key, op in operations.items():
        assert any(s.startswith("5") for s in op["responses"]), key
        assert "503" in op["responses"], key


def test_retry_after_declared_where_needed(operations, resolve, catalog):
    for key, op in operations.items():
        for status, codes in _codes_by_status(op, resolve).items():
            if any(catalog[c].get("retryAfter") for c in codes):
                headers = resolve(op["responses"][status]).get("headers", {})
                assert "Retry-After" in headers, (key, status)
                h = resolve(headers["Retry-After"])
                assert h["schema"]["type"] == "integer"
                # Обязателен, если все коды ответа его требуют.
                if all(catalog[c].get("retryAfter") for c in codes):
                    assert h["required"] is True, (key, status)


def test_unauthorized_codes_by_subject(operations, resolve):
    for key, op in operations.items():
        subject = op["x-subject"]
        codes = _codes_by_status(op, resolve).get("401")
        if subject == "guest":
            assert codes == UNAUTHORIZED_GUEST, key
        elif subject in ("account", "any"):
            assert codes == UNAUTHORIZED_ACCOUNT, key
        elif key == ("post", "/v1/auth/refresh"):
            assert codes == {"refresh_token_invalid", "session_revoked", "account_deleted"}
        else:
            assert codes is None, key


def test_account_only_operations_return_account_required(operations, resolve):
    for key, op in operations.items():
        codes = _codes_by_status(op, resolve)
        if op["x-subject"] == "account":
            assert codes.get("403") == {"account_required"}, key
        else:
            assert "403" not in codes, key


@pytest.mark.parametrize("key", sorted(REQUIRED_BY_OPERATION))
def test_operation_specific_codes(operations, resolve, key):
    declared = set().union(*_codes_by_status(operations[key], resolve).values())
    assert REQUIRED_BY_OPERATION[key] <= declared


def test_error_examples_match_catalog(spec, catalog):
    for name, ex in spec["components"]["examples"].items():
        err = ex["value"].get("error") if isinstance(ex["value"], dict) else None
        if not err:
            continue
        entry = catalog[err["code"]]
        assert err["status"] == entry["status"], name
        assert err["retryable"] == entry["retryable"], name


def test_envelope_shape(schema_validator):
    v = schema_validator("/components/schemas/ErrorEnvelope")
    ok = {
        "error": {
            "code": "resync_required",
            "message": "Resync required",
            "status": 410,
            "retryable": False,
            "requestId": "req_" + "0" * 32,
            "details": [],
        }
    }
    assert not list(v.iter_errors(ok))
    for field in ("code", "message", "status", "retryable", "requestId", "details"):
        broken = {"error": {k: val for k, val in ok["error"].items() if k != field}}
        assert list(v.iter_errors(broken)), field
    unknown = {"error": {**ok["error"], "code": "something_new"}}
    assert list(v.iter_errors(unknown))


def test_shared_codes_keep_legacy_semantics(catalog):
    """Коды, общие с текущей моделью 09 — Error Model, сохраняют статус и retryable."""
    from app.core.errors import ERROR_CATALOG as LEGACY

    shared = set(catalog) & set(LEGACY)
    assert {"invalid_request", "validation_failed", "rate_limited", "file_too_large"} <= shared
    for code in shared:
        status, retryable, _ = LEGACY[code]
        assert (catalog[code]["status"], catalog[code]["retryable"]) == (status, retryable), code
