"""Тест-векторы контракта: правило комментария (§3.3), id отметки дня (§4), реестр системных категорий (§3.2).

Здесь же — эталонная реализация правила комментария на Python: в E3 серверная обрезка
обязана совпасть с ней, iOS проверяет свою реализацию на тех же векторах.
"""

import json
import unicodedata
import uuid

import pytest
import regex
from conftest import CONTRACT

COMMENT_MAX = 200


def normalize_comment(s: str) -> str | None:
    s = unicodedata.normalize("NFC", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cc")
    start, end = 0, len(s)
    while start < end and unicodedata.category(s[start]).startswith("Z"):
        start += 1
    while end > start and unicodedata.category(s[end - 1]).startswith("Z"):
        end -= 1
    return s[start:end] or None


def truncate_comment(s: str | None, limit: int = COMMENT_MAX) -> str | None:
    if s is None:
        return None
    out, size = [], 0
    for g in regex.findall(r"\X", s):
        if size + len(g) > limit:
            break
        out.append(g)
        size += len(g)
    return "".join(out) or None


def _ids(vectors):
    return [c["id"] for c in vectors["cases"]]


def test_comment_vectors_unique_ids(comment_vectors):
    ids = _ids(comment_vectors)
    assert len(ids) == len(set(ids)) and len(ids) >= 25
    assert comment_vectors["maxScalars"] == COMMENT_MAX


CASES = json.loads((CONTRACT / "vectors" / "comment.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(params=CASES, ids=[c["id"] for c in CASES])
def case(request):
    return request.param


def test_comment_normalization(case):
    assert normalize_comment(case["input"]) == case["normalized"], case["id"]


def test_comment_scalar_count(case):
    n = case["normalized"]
    assert (len(n) if n else 0) == case["scalarCount"], case["id"]


def test_comment_sync_validity(case):
    assert (case["scalarCount"] <= COMMENT_MAX) == case["syncValid"], case["id"]


def test_comment_import_truncation(case):
    t = truncate_comment(case["normalized"])
    assert t == case["importTruncated"], case["id"]
    if t is not None:
        assert len(t) <= COMMENT_MAX
        assert case["normalized"].startswith(t)


def test_comment_vectors_against_schema(case, schema_validator):
    """maxLength JSON Schema считает кодовые точки — то же, что правило §3.3."""
    v = schema_validator("/components/schemas/ExpensePayload")
    payload = {
        "amount": "1.00",
        "categoryId": "00000000-0000-4000-8000-000000000099",
        "date": "2026-10-11",
        "comment": case["normalized"],
    }
    assert (not list(v.iter_errors(payload))) == case["syncValid"], case["id"]


def test_python_strip_equivalence(comment_vectors):
    # После удаления Cc обрезка категорий Z* совпадает с str.strip() — так пишет сервер в E3.
    for c in comment_vectors["cases"]:
        s = unicodedata.normalize("NFC", c["input"])
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Cc").strip()
        assert (s or None) == c["normalized"], c["id"]


def test_plan_emoji_examples(comment_vectors):
    by_input = {c["input"]: c["scalarCount"] for c in comment_vectors["cases"]}
    assert by_input["😀"] == 1
    assert by_input["👍🏽"] == 2
    assert by_input["👨‍👩‍👧"] == 5


def test_day_mark_ids(day_mark_vectors, spec):
    ns = uuid.UUID(day_mark_vectors["namespace"])
    assert str(ns) == spec["x-contract"]["dayMarkNamespace"]
    assert len(day_mark_vectors["cases"]) >= 5
    for c in day_mark_vectors["cases"]:
        expected = uuid.uuid5(ns, c["date"])
        assert c["entityId"] == str(expected), c["date"]
        assert expected.version == 5


def test_day_mark_examples_use_namespace(spec, day_mark_vectors):
    by_date = {c["date"]: c["entityId"] for c in day_mark_vectors["cases"]}
    for rec in spec["components"]["examples"]["PullResponse"]["value"]["records"]:
        if rec["entityType"] == "day_mark":
            assert rec["entityId"] == by_date[rec["payload"]["date"]]


def test_system_category_registry(registry, spec):
    ids = [c["id"] for c in registry["categories"]]
    assert len(ids) == len(set(ids))
    for cid in ids:
        assert str(uuid.UUID(cid)) == cid  # канонический вид, нижний регистр
    assert registry["fallbackCategoryId"] == "00000000-0000-4000-8000-000000000099"
    non_archivable = [c["id"] for c in registry["categories"] if not c["archivable"]]
    assert non_archivable == [registry["fallbackCategoryId"]]
    assert spec["x-contract"]["systemCategoryRegistry"] == "contract/system-categories.json"


def test_registry_ids_never_collide_with_day_marks(registry, day_mark_vectors):
    # UUIDv5 отметок не пересекается с фиксированными id системных категорий.
    ids = {c["id"] for c in registry["categories"]}
    assert not ids & {c["entityId"] for c in day_mark_vectors["cases"]}


def test_system_category_examples_use_registry(spec, registry):
    ids = {c["id"] for c in registry["categories"]}
    examples = spec["components"]["examples"]
    for m in examples["PushRequest"]["value"]["mutations"]:
        if m["entityType"] == "category" and m["payload"]["kind"] == "system":
            assert m["entityId"] in ids
    for r in examples["PullResponse"]["value"]["records"]:
        if r["entityType"] == "category" and r["payload"]["kind"] == "system":
            assert r["entityId"] in ids


def _seed_0002() -> list[tuple]:
    import ast

    from conftest import ROOT

    src = (ROOT / "migrations" / "versions" / "0002_system_categories.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "SYSTEM_CATEGORIES":
            return ast.literal_eval(node.value)
    raise AssertionError("SYSTEM_CATEGORIES not found")


def test_registry_is_draft_until_approved(registry):
    """Список не утверждён: статус draft, без источника решения. Утверждение меняет оба поля."""
    if registry["status"] == "approved":
        assert registry["approval"]["source"] and registry["approval"]["approvedBy"]
    else:
        assert registry["status"] == "draft"
        assert registry["approval"] == {"approvedBy": None, "approvedAt": None, "source": None}
        assert all("черновик" in c["source"] for c in registry["categories"])


def test_registry_matches_draft_seed(registry):
    """Пока список — черновик, реестр совпадает с seed 0002 (не расходится молча)."""
    if registry["status"] != "draft":
        pytest.skip("реестр утверждён отдельно от seed")
    seed = [
        {"id": i, "nameRu": n, "emoji": e, "colorIndex": c, "sortOrder": o} for i, n, e, c, o in _seed_0002()
    ]
    fields = ("id", "nameRu", "emoji", "colorIndex", "sortOrder")
    assert [{k: c[k] for k in fields} for c in registry["categories"]] == seed


def test_registry_definitions_shape(registry):
    keys = [c["key"] for c in registry["categories"]]
    assert len(keys) == len(set(keys))
    assert [c["colorIndex"] for c in registry["categories"]] == sorted(
        {c["colorIndex"] for c in registry["categories"]}
    )
    assert all(0 <= c["colorIndex"] <= 11 for c in registry["categories"])
    orders = [c["sortOrder"] for c in registry["categories"]]
    assert orders == sorted(orders) and len(orders) == len(set(orders))
    other = next(c for c in registry["categories"] if c["id"] == registry["fallbackCategoryId"])
    assert other["archivable"] is False and other["key"] == "other"
    assert other["sortOrder"] == max(orders)


def test_registry_keys_match_backend_rules(registry):
    """Ключи, уже используемые правилами категоризации, указывают на те же UUID."""
    from app.modules.imports.categorization import SYS

    by_key = {c["key"]: c["id"] for c in registry["categories"]}
    assert {k: str(v) for k, v in SYS.items()} == {k: by_key[k] for k in SYS}


def test_spec_marks_registry_status(spec, registry):
    assert spec["x-contract"]["systemCategoryRegistryStatus"] == registry["status"]


def test_swift_reference_exists():
    from conftest import CONTRACT

    src = (CONTRACT / "swift" / "comment_rule_check.swift").read_text(encoding="utf-8")
    assert "generalCategory" in src and "JSONDecoder" in src
