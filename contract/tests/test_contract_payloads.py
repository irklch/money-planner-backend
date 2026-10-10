"""Контрактные сценарии: корректные и некорректные запросы/ответы против схем контракта.

Нарушение схемы payload в push сервер превращает в rejected/invalid по мутации (а не в ошибку
запроса); здесь проверяется, что такие мутации схема считает некорректными.
"""

import copy
import uuid

import pytest

DEVICE = "3f1c8a52-6d0e-4f7a-9b21-0c5d8e7f6a10"
TS = "2026-10-11T09:00:00+03:00"
OTHER = "00000000-0000-4000-8000-000000000099"


def _mutation(entity_type: str, payload, op: str = "upsert", **kw) -> dict:
    m = {
        "mutationId": str(uuid.uuid4()),
        "entityType": entity_type,
        "entityId": str(uuid.uuid4()),
        "op": op,
        "baseVersion": 0,
        "createdAt": TS,
        "updatedAt": TS,
        "deletedAt": TS if op == "delete" else None,
        "schemaVersion": 1,
        "payload": payload,
    }
    m.update(kw)
    return m


EXPENSE = {"amount": "250.50", "categoryId": OTHER, "date": "2026-10-11", "comment": "Кофе"}
USER_CATEGORY = {"kind": "user", "name": "Кофе", "emoji": "☕", "colorIndex": 4, "isArchived": False}
SYSTEM_CATEGORY = {"kind": "system", "isArchived": True}
DAY_MARK = {"date": "2026-10-10", "isMarked": True}


def _errors(validator, value) -> list[str]:
    return [e.message for e in validator.iter_errors(value)]


@pytest.fixture(scope="module")
def mutation_v(schema_validator):
    return schema_validator("/components/schemas/Mutation")


@pytest.fixture(scope="module")
def push_v(schema_validator):
    return schema_validator("/components/schemas/PushRequest")


VALID_MUTATIONS = {
    "expense_upsert": _mutation("expense", EXPENSE),
    "expense_upsert_without_comment": _mutation(
        "expense", {k: v for k, v in EXPENSE.items() if k != "comment"}
    ),
    "expense_upsert_null_comment": _mutation("expense", {**EXPENSE, "comment": None}),
    "expense_delete": _mutation("expense", None, op="delete", baseVersion=17),
    "category_user": _mutation("category", USER_CATEGORY),
    "category_user_sort_order": _mutation("category", {**USER_CATEGORY, "sortOrder": 3}),
    "category_system_archive": _mutation("category", SYSTEM_CATEGORY, entityId=OTHER),
    "day_mark_set": _mutation("day_mark", DAY_MARK),
    "day_mark_unset": _mutation("day_mark", {**DAY_MARK, "isMarked": False}),
    "max_amount": _mutation("expense", {**EXPENSE, "amount": "999999999.99"}),
    "min_amount": _mutation("expense", {**EXPENSE, "amount": "0.01"}),
    "comment_200_scalars": _mutation("expense", {**EXPENSE, "comment": "a" * 200}),
    "comment_emoji_counts_scalars": _mutation("expense", {**EXPENSE, "comment": "a" * 195 + "👨‍👩‍👧"}),
}


@pytest.mark.parametrize("name", sorted(VALID_MUTATIONS))
def test_valid_mutations(mutation_v, name):
    assert _errors(mutation_v, VALID_MUTATIONS[name]) == []


INVALID_MUTATIONS = {
    "amount_zero": _mutation("expense", {**EXPENSE, "amount": "0.00"}),
    "amount_one_decimal": _mutation("expense", {**EXPENSE, "amount": "250.5"}),
    "amount_no_decimals": _mutation("expense", {**EXPENSE, "amount": "250"}),
    "amount_negative": _mutation("expense", {**EXPENSE, "amount": "-1.00"}),
    "amount_too_large": _mutation("expense", {**EXPENSE, "amount": "1000000000.00"}),
    "amount_leading_zero": _mutation("expense", {**EXPENSE, "amount": "01.00"}),
    "amount_number": _mutation("expense", {**EXPENSE, "amount": 250.5}),
    "comment_201_scalars": _mutation("expense", {**EXPENSE, "comment": "a" * 201}),
    "comment_emoji_over_limit": _mutation("expense", {**EXPENSE, "comment": "a" * 199 + "👍🏽"}),
    "comment_empty_instead_of_null": _mutation("expense", {**EXPENSE, "comment": ""}),
    "date_with_time": _mutation("expense", {**EXPENSE, "date": "2026-10-11T00:00:00Z"}),
    "category_id_not_uuid": _mutation("expense", {**EXPENSE, "categoryId": "groceries"}),
    "expense_extra_field": _mutation("expense", {**EXPENSE, "source": "import"}),
    "expense_upsert_with_deleted_at": _mutation("expense", EXPENSE, deletedAt=TS),
    "expense_delete_with_payload": _mutation("expense", EXPENSE, op="delete"),
    "expense_delete_without_deleted_at": _mutation("expense", None, op="delete", deletedAt=None),
    "category_delete": _mutation("category", None, op="delete"),
    "category_name_25": _mutation("category", {**USER_CATEGORY, "name": "Я" * 25}),
    "category_name_empty": _mutation("category", {**USER_CATEGORY, "name": ""}),
    "category_color_12": _mutation("category", {**USER_CATEGORY, "colorIndex": 12}),
    "category_color_negative": _mutation("category", {**USER_CATEGORY, "colorIndex": -1}),
    "category_emoji_empty": _mutation("category", {**USER_CATEGORY, "emoji": ""}),
    "category_without_kind": _mutation("category", {k: v for k, v in USER_CATEGORY.items() if k != "kind"}),
    "category_system_with_name": _mutation("category", {**SYSTEM_CATEGORY, "name": "Транспорт"}),
    "category_unknown_kind": _mutation("category", {**SYSTEM_CATEGORY, "kind": "shared"}),
    "day_mark_delete": _mutation("day_mark", None, op="delete"),
    "day_mark_without_flag": _mutation("day_mark", {"date": "2026-10-10"}),
    "unknown_entity_type": _mutation("app_settings", {"theme": "dark"}),
    "unknown_op": _mutation("expense", EXPENSE, op="patch"),
    "negative_base_version": _mutation("expense", EXPENSE, baseVersion=-1),
    "schema_version_zero": _mutation("expense", EXPENSE, schemaVersion=0),
    "naive_timestamp": _mutation("expense", EXPENSE, updatedAt="2026-10-11T09:00:00"),
    "mutation_id_not_uuid": _mutation("expense", EXPENSE, mutationId="m-1"),
}


@pytest.mark.parametrize("name", sorted(INVALID_MUTATIONS))
def test_invalid_mutations(mutation_v, name):
    assert _errors(mutation_v, INVALID_MUTATIONS[name]), name


def test_unknown_schema_version_is_not_a_schema_error(mutation_v):
    # Неизвестная версия — 426 upgrade_required, а не 422: схема её пропускает.
    assert _errors(mutation_v, _mutation("expense", EXPENSE, schemaVersion=2)) == []


def test_push_request_limits(push_v):
    one = [VALID_MUTATIONS["expense_upsert"]]
    assert _errors(push_v, {"deviceId": DEVICE, "mutations": one}) == []
    assert _errors(push_v, {"deviceId": DEVICE, "mutations": []})
    assert _errors(push_v, {"deviceId": DEVICE, "mutations": one * 500}) == []
    assert _errors(push_v, {"deviceId": DEVICE, "mutations": one * 501})
    assert _errors(push_v, {"deviceId": "my phone", "mutations": one})
    assert _errors(push_v, {"deviceId": DEVICE, "mutations": one, "userId": str(uuid.uuid4())})


@pytest.fixture(scope="module")
def record_v(schema_validator):
    return schema_validator("/components/schemas/SyncRecord")


def _record(entity_type: str, payload, deleted_at=None) -> dict:
    return {
        "entityType": entity_type,
        "entityId": str(uuid.uuid4()),
        "version": 7,
        "createdAt": "2026-10-11T06:00:00Z",
        "updatedAt": "2026-10-11T06:00:00Z",
        "deletedAt": deleted_at,
        "deviceId": DEVICE,
        "schemaVersion": 1,
        "payload": payload,
    }


def test_records(record_v):
    assert _errors(record_v, _record("expense", EXPENSE)) == []
    assert _errors(record_v, _record("expense", None, deleted_at="2026-10-11T06:00:00Z")) == []
    assert _errors(record_v, _record("category", USER_CATEGORY)) == []
    assert _errors(record_v, _record("category", SYSTEM_CATEGORY)) == []
    assert _errors(record_v, _record("day_mark", DAY_MARK)) == []
    # Категории и отметки не имеют tombstone.
    assert _errors(record_v, _record("category", USER_CATEGORY, deleted_at="2026-10-11T06:00:00Z"))
    assert _errors(record_v, _record("day_mark", None))
    assert _errors(record_v, _record("note", {"text": "x"}))
    assert _errors(record_v, {**_record("expense", EXPENSE), "version": 0})


def test_mutation_result_shapes(schema_validator):
    v = schema_validator("/components/schemas/MutationResult")
    mid = str(uuid.uuid4())
    base = {"mutationId": mid, "conflict": False, "replayed": False}
    assert _errors(v, {**base, "status": "applied", "version": 45}) == []
    assert _errors(v, {**base, "status": "noop"}) == []
    for reason in ("conflict", "deleted", "invalid", "mutation_id_reused"):
        assert _errors(v, {**base, "status": "rejected", "reason": reason}) == [], reason
    assert (
        _errors(
            v, {**base, "status": "rejected", "reason": "invalid", "details": [{"code": "op_not_allowed"}]}
        )
        == []
    )
    assert (
        _errors(
            v,
            {
                **base,
                "status": "rejected",
                "reason": "conflict",
                "record": _record("category", USER_CATEGORY),
            },
        )
        == []
    )
    assert _errors(v, {**base, "status": "failed"})
    assert _errors(v, {"mutationId": mid, "status": "applied"})


def test_pull_response(schema_validator):
    v = schema_validator("/components/schemas/PullResponse")
    page = {"records": [_record("expense", EXPENSE)], "nextCursor": 7, "hasMore": True, "horizon": 0}
    assert _errors(v, page) == []
    assert _errors(v, {**page, "records": [_record("expense", EXPENSE)] * 501})
    assert _errors(v, {k: val for k, val in page.items() if k != "horizon"})


def test_query_parameter_schemas(spec, schema_validator):
    limit = schema_validator("/components/parameters/Limit/schema")
    cursor = schema_validator("/components/parameters/Cursor/schema")
    assert _errors(limit, 500) == [] and _errors(limit, 501) and _errors(limit, 0)
    assert _errors(cursor, 0) == [] and _errors(cursor, -1)


SESSION_TOKENS = {
    "accessToken": "a.b.c",
    "accessTokenExpiresAt": "2026-10-11T12:15:00Z",
    "refreshToken": "r" * 32,
    "refreshTokenExpiresAt": "2027-10-11T12:00:00Z",
}


def test_sessions(schema_validator):
    v = schema_validator("/components/schemas/Session")
    guest = {**SESSION_TOKENS, "subjectKind": "guest", "deviceId": DEVICE}
    account = {**guest, "subjectKind": "account", "accountId": str(uuid.uuid4())}
    assert _errors(v, guest) == []
    assert _errors(v, account) == []
    assert _errors(v, {**guest, "subjectKind": "account"})  # аккаунту нужен accountId
    assert _errors(v, {k: val for k, val in guest.items() if k != "deviceId"})

    apple = schema_validator("/components/schemas/AppleSignInResponse")
    assert _errors(apple, {**account, "sync": {"lastVersion": 0, "hasData": False}}) == []
    assert _errors(apple, account)  # sync обязателен
    assert _errors(apple, {**guest, "sync": {"lastVersion": 0, "hasData": False}})


def test_me(schema_validator):
    v = schema_validator("/components/schemas/Me")
    imports = {"used": 0, "limit": 3, "state": "free", "enforced": False}
    guest = {"subjectKind": "guest", "deviceId": DEVICE, "features": {"imports": imports}}
    account = {
        **guest,
        "subjectKind": "account",
        "accountId": str(uuid.uuid4()),
        "sync": {"lastVersion": 3},
    }
    assert _errors(v, guest) == []
    assert _errors(v, account) == []
    assert _errors(v, {**account, "sync": None})
    assert _errors(v, {**guest, "features": {"imports": {**imports, "state": "blocked"}}})
    assert _errors(
        v, {**guest, "features": {"imports": {k: x for k, x in imports.items() if k != "enforced"}}}
    )


def test_deletion(schema_validator):
    acc = schema_validator("/components/schemas/DeletionAccepted")
    st = schema_validator("/components/schemas/DeletionStatus")
    did = str(uuid.uuid4())
    for state in ("pending", "revoking", "deleting", "done", "failed_retrying"):
        assert _errors(acc, {"deletionId": did, "state": state}) == [], state
    assert _errors(acc, {"deletionId": did, "state": "cancelled"})
    for rev in ("pending", "done", "skipped_no_token", "not_required"):
        status = {"deletionId": did, "state": "done", "appleTokenRevocation": rev, "updatedAt": TS}
        assert _errors(st, status) == [], rev
    req = schema_validator("/components/schemas/DeleteAccountRequest")
    assert _errors(req, {}) == [] and _errors(req, {"authorizationCode": "c1"}) == []


def test_auth_requests(schema_validator):
    guest = schema_validator("/components/schemas/GuestSessionRequest")
    assert _errors(guest, {"platform": "ios", "appVersion": "1.0.0"}) == []
    assert _errors(guest, {"platform": "android", "appVersion": "1.0.0"}) == []
    assert _errors(guest, {"platform": "web", "appVersion": "1.0.0"})
    apple = schema_validator("/components/schemas/AppleSignInRequest")
    ok = {"identityToken": "x.y.z", "authorizationCode": "c", "nonce": "n" * 32}
    assert _errors(apple, ok) == []
    for field in ok:
        assert _errors(apple, {k: v for k, v in ok.items() if k != field}), field
    assert _errors(apple, {**ok, "nonce": "short"})
    refresh = schema_validator("/components/schemas/RefreshRequest")
    assert _errors(refresh, {"refreshToken": "r" * 32}) == [] and _errors(refresh, {})


def test_parse_form_and_result(schema_validator, spec):
    form = schema_validator("/components/schemas/ParseStatementForm")
    cats = [{"id": OTHER, "name": "Другое"}]
    hints = [{"merchantKey": "пятерочка", "categoryId": OTHER}]
    assert _errors(form, {"file": "<bytes>", "categories": cats, "categoryHints": hints}) == []
    assert _errors(form, {"file": "<bytes>", "categories": cats}) == []
    assert _errors(form, {"file": "<bytes>"})  # categories обязательны
    assert _errors(form, {"file": "<bytes>", "categories": cats, "categoryHints": hints * 501})
    assert _errors(
        form,
        {
            "file": "<bytes>",
            "categories": cats,
            "categoryHints": [{"merchantKey": "k" * 41, "categoryId": OTHER}],
        },
    )

    result = schema_validator("/components/schemas/ParseResult")
    example = copy.deepcopy(spec["components"]["examples"]["ParseResult"]["value"])
    assert _errors(result, example) == []
    for field in ("importId", "confirmed", "parserVersion", "imports"):
        assert _errors(result, {k: v for k, v in example.items() if k != field}), field
    example["operations"][0]["comment"] = "a" * 201
    assert _errors(result, example)


def test_apple_notification(schema_validator):
    v = schema_validator("/components/schemas/AppleNotificationRequest")
    assert _errors(v, {"payload": "eyJ.eyJ.sig"}) == []
    assert _errors(v, {}) and _errors(v, {"payload": ""})
