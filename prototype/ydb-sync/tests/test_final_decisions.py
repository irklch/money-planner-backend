"""Сценарии утверждённых решений: правило deviceId, offline-цепочки, полная перезагрузка после 410,
архивирование категорий, единая модель Expense, только расходы и категории в sync.

Идут через HTTP API (FastAPI in-process) и локальную YDB. Все данные синтетические.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from client.client import LocalError, SyncClient
from client.transport import HttpTransport

from .conftest import USER_A, SkewClock, assert_converged, server_view, token

pytestmark = pytest.mark.ydb


def _iso(v: dt.datetime) -> str:
    return v.isoformat().replace("+00:00", "Z")


# Сырая мутация расхода — для сценариев, которые клиент штатно не порождает.
def _raw_expense(eid: str, cat: str, base: int, ts: dt.datetime, amount: str) -> dict:
    return {
        "mutationId": str(uuid.uuid4()),
        "entityType": "expense",
        "entityId": eid,
        "op": "upsert",
        "baseVersion": base,
        "createdAt": _iso(ts),
        "updatedAt": _iso(ts),
        "deletedAt": None,
        "payload": {"amount": amount, "categoryId": cat, "date": "2026-10-01"},
    }


async def _server_live(store, user_id: str) -> dict:
    return {k: p for k, (p, deleted) in server_view(await store.server_state(user_id)).items() if not deleted}


# --- правило deviceId ----------------------------------------------------------------------


async def test_stale_independent_edit_from_same_device_does_not_overwrite(device, store, http):
    """Регрессия. Устройство phone-a — автор последней правки (версия 2). Оно же присылает
    независимую правку, основанную на версии 1 и сделанную раньше по времени (например, outbox,
    восстановленный из резервной копии). Раньше совпадение deviceId давало ей приоритет, и она
    перезаписывала актуальную правку. Теперь это обычный конкурентный конфликт: она отклоняется."""
    a = device(USER_A, "phone-a")
    cat = a.create_category("Еда")
    e = a.create_expense("100.00", cat, "2026-10-01")
    await a.sync()
    v1 = (await store.server_state(USER_A))[("expense", e)].version
    a.update("expense", e, amount="200.00")
    await a.sync()
    t = HttpTransport(http, token(USER_A))
    stale = _raw_expense(e, cat, v1, dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10), "999.00")
    resp = await t.push({"deviceId": "phone-a", "mutations": [stale]})
    r = resp["results"][0]
    assert r["status"] == "rejected" and r["reason"] == "conflict" and r["conflict"]
    assert r["record"]["payload"]["amount"] == "200.00"
    assert (await store.server_state(USER_A))[("expense", e)].payload["amount"] == "200.00"


@pytest.mark.parametrize("skew_days", [0, 1])
async def test_sequential_offline_edits_of_one_device_across_batches(device, store, skew_days):
    """Офлайн: создание и четыре правки одной записи, затем отправка по ОДНОЙ мутации в запросе.
    Все применяются без конфликтов, итог — последняя правка. С часами на сутки вперёд (обе метки
    обрезаются до now+5 мин) — так же: приоритет даёт актуальная база, а не время и не deviceId."""
    clock = SkewClock(dt.timedelta(days=skew_days))
    a = device(USER_A, "phone-a", clock, push_batch=1)
    b = device(USER_A, "ipad-a")
    a.transport.offline = True
    cat = a.create_category("Еда")
    e = a.create_expense("1.00", cat, "2026-10-01")
    for amount in ("2.00", "3.00", "4.00", "5.00"):
        clock.advance(1)
        a.update("expense", e, amount=amount)
    a.transport.offline = False
    r = await a.sync()
    assert r.ok and r.push_requests == 6 and r.applied == 6 and r.conflicts == 0 and r.rejected == 0
    await b.sync()
    assert b.visible("expense")[e]["amount"] == "5.00"
    await assert_converged(store, USER_A, a, b)


async def test_lost_response_then_more_offline_edits(device, store):
    """Ответ на первую правку потерян, устройство продолжает править офлайн. Повтор возвращает
    сохранённый результат, следующая правка перебазируется — без ложного конфликта."""
    a = device(USER_A, "phone-a")
    e = a.create_expense("1.00", a.create_category("Еда"), "2026-10-01")
    await a.sync()
    a.update("expense", e, amount="2.00")
    a.transport.drop_push_responses = 1
    assert not (await a.sync()).ok
    a.update("expense", e, amount="3.00")
    r = await a.sync()
    assert r.ok and r.conflicts == 0 and r.rejected == 0
    final = await assert_converged(store, USER_A, a)
    assert final[("expense", e)][0]["amount"] == "3.00"


# --- полная перезагрузка после 410 ---------------------------------------------------------


async def _seed(a: SyncClient, n: int) -> tuple[str, list[str]]:
    cat = a.create_category("Еда")
    ids = [a.create_expense(f"{i + 1}.00", cat, "2026-10-01", f"синтетика {i}") for i in range(n)]
    assert (await a.sync()).ok
    return cat, ids


async def test_resync_after_410_keeps_local_changes_and_does_not_restore_deleted(device, store):
    """Устройство A долго офлайн. За это время B удалил расходы, их tombstones очищены, B правил
    другие. У A в outbox: новый расход, правка удалённого (и очищенного) расхода, правка, конфликтующая
    с правкой B, и обычная правка. После sync: новый расход есть, удалённые не восстановлены,
    конфликт решён по обычным правилам, A совпадает с сервером."""
    ca, cb = SkewClock(), SkewClock()
    a, b = device(USER_A, "phone-a", ca, pull_limit=3), device(USER_A, "ipad-a", cb)
    cat, e = await _seed(a, 8)
    await b.sync()
    a.transport.offline = True
    # B: удаляет e0, e1 (у A правок нет) и e2 (у A будет офлайн-правка), правит e3.
    for x in e[:3]:
        b.delete("expense", x)
    b.update("expense", e[3], amount="300.00")
    assert (await b.sync()).ok
    await store.purge_tombstones(USER_A, await store.last_version(USER_A))
    # A офлайн, позже по времени, чем правки B.
    ca.advance(60)
    new = a.create_expense("77.00", cat, "2026-10-05", "новый офлайн")
    a.update("expense", e[2], amount="222.00")  # удалён и очищен на сервере
    a.update("expense", e[3], comment="правка A")  # конкурентна правке B, но позже → побеждает A
    a.update("expense", e[4], amount="444.00")  # без конфликта
    assert a.outbox_size() == 4
    a.transport.offline = False
    r = await a.sync()
    assert r.ok and r.resyncs == 1
    live = await assert_converged(store, USER_A, a, b, live=True)
    assert ("expense", new) in live
    for x in e[:3]:
        assert ("expense", x) not in live and x not in a.visible("expense"), "удалённый расход восстановлен"
    assert live[("expense", e[3])]["comment"] == "правка A"
    assert live[("expense", e[4])]["amount"] == "444.00"


async def test_resync_interrupted_and_app_restart_preserve_outbox(device, store):
    """410 → полная перезагрузка обрывается после двух страниц. Пользователь продолжает работать
    офлайн, приложение перезапускается. Outbox цел, перезагрузка продолжается с сохранённого места,
    затем локальные изменения согласуются по обычным правилам."""
    a, b = device(USER_A, "phone-a", pull_limit=3), device(USER_A, "ipad-a")
    cat, e = await _seed(a, 14)
    await b.sync()
    for x in e[:4]:
        b.delete("expense", x)
    b.update("expense", e[5], amount="555.00")
    assert (await b.sync()).ok
    await store.purge_tombstones(USER_A, await store.last_version(USER_A))
    live_on_server = len(await _server_live(store, USER_A))  # 1 категория + 10 расходов

    a.transport.fail_pull_after(2)
    r = await a.sync()
    assert not r.ok and r.resyncs == 1 and a.resync_pending
    progress = int(a._meta("resync_cursor"))
    assert progress > 0
    # Работа офлайн во время незавершённой перезагрузки, затем «падение» приложения.
    a.transport.offline = True
    new = a.create_expense("1.23", cat, "2026-10-06", "во время resync")
    a.update("expense", e[3], amount="333.00")  # удалён и очищен на сервере
    a.update("expense", e[6], amount="666.00")
    outbox_before = [r[0] for r in a.db.execute("SELECT mutation_id FROM outbox ORDER BY seq")]
    del a
    a = device(USER_A, "phone-a", pull_limit=3)
    assert a.resync_pending and int(a._meta("resync_cursor")) == progress
    assert [r[0] for r in a.db.execute("SELECT mutation_id FROM outbox ORDER BY seq")] == outbox_before

    r = await a.sync()
    assert r.ok
    assert r.pulled < live_on_server, "перезагрузка началась заново вместо продолжения"
    live = await assert_converged(store, USER_A, a, b, live=True)
    assert ("expense", new) in live
    assert all(("expense", x) not in live for x in e[:4])
    assert live[("expense", e[6])]["amount"] == "666.00"
    assert a.live() == live


async def test_resync_restarts_from_zero_when_horizon_moves_mid_resync(device, store):
    """Перезагрузка прервана; за это время B удалил расход, который A уже успел получить в
    перезагрузке, и tombstone очищен. Продолжение получает 410 и начинает заново — иначе этот
    расход остался бы у A живым навсегда."""
    a, b = device(USER_A, "phone-a", pull_limit=2), device(USER_A, "ipad-a")
    _, e = await _seed(a, 9)
    await b.sync()
    b.delete("expense", e[8])
    await b.sync()
    await store.purge_tombstones(USER_A, await store.last_version(USER_A))
    a.transport.fail_pull_after(2)
    assert not (await a.sync()).ok and a.resync_pending
    assert e[0] in a.visible("expense")  # уже получен в первой странице перезагрузки
    b.delete("expense", e[0])
    await b.sync()
    await store.purge_tombstones(USER_A, await store.last_version(USER_A))
    r = await a.sync()
    assert r.ok and r.resyncs == 1  # 410 при продолжении → начать с нуля
    live = await assert_converged(store, USER_A, a, b, live=True)
    assert ("expense", e[0]) not in live and e[0] not in a.visible("expense")


async def test_new_device_full_load_after_purge_with_many_pages(device, store):
    """Регрессия протокола: после очистки tombstones загрузка с нуля страницами получала 410 на
    второй странице (курсор < горизонта) и зацикливалась. Клиент передаёт известный горизонт."""
    a = device(USER_A, "phone-a")
    _, e = await _seed(a, 12)
    a.delete("expense", e[0])
    await a.sync()
    await store.purge_tombstones(USER_A, await store.last_version(USER_A))
    c = device(USER_A, "phone-new", pull_limit=2)
    r = await c.sync()
    assert r.ok and r.resyncs == 0 and r.pages == 6
    assert c.live() == await _server_live(store, USER_A)
    assert len(c.visible("expense")) == 11


async def test_pull_410_depends_on_known_horizon(device, store, http):
    a = device(USER_A, "phone-a")
    await _seed(a, 3)
    await store.purge_tombstones(USER_A, 3)
    t = HttpTransport(http, token(USER_A))
    from client.transport import ResyncRequiredError

    with pytest.raises(ResyncRequiredError):
        await t.pull(1, 10)  # курсор 1 < горизонта 3, а клиент знал горизонт 0
    page = await t.pull(1, 10, horizon=3)  # горизонт не сдвигался с прошлого ответа
    assert page["horizon"] == 3 and page["records"]
    assert (await t.pull(0, 10))["records"]  # с нуля — всегда можно


# --- категории: удаление = архивирование ---------------------------------------------------


async def test_archived_category_syncs_and_keeps_expenses(device, store):
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a")
    cat = a.create_category("Кафе", "☕")
    other = a.create_category("Транспорт")
    e1 = a.create_expense("300.00", cat, "2026-09-01", "капучино")
    e2 = a.create_expense("450.00", cat, "2026-09-15")
    e3 = a.create_expense("60.00", other, "2026-09-16")
    await a.sync()
    await b.sync()
    a.archive_category(cat)
    await a.sync()
    await b.sync()
    # Архивное состояние дошло до B, категория осталась (для истории и аналитики).
    assert b.visible("category")[cat] == {"name": "Кафе", "emoji": "☕", "isArchived": True}
    assert cat not in b.active_categories() and other in b.active_categories()
    # Расходы сохранили связь с категорией; их можно править, оставаясь в ней.
    assert {b.visible("expense")[x]["categoryId"] for x in (e1, e2)} == {cat}
    b.update("expense", e1, amount="310.00")
    # Для новых расходов и переноса — недоступна.
    with pytest.raises(LocalError):
        b.create_expense("1.00", cat, "2026-10-01")
    with pytest.raises(LocalError):
        b.update("expense", e3, categoryId=cat)
    b.update("expense", e2, categoryId=cat, comment="та же категория")  # остаться в ней можно
    await b.sync()
    final = await assert_converged(store, USER_A, a, b)
    assert final[("category", cat)] == ({"name": "Кафе", "emoji": "☕", "isArchived": True}, False)
    assert final[("expense", e1)][0]["categoryId"] == cat


async def test_category_cannot_be_deleted(device, store, http):
    a = device(USER_A, "phone-a")
    cat = a.create_category("Еда")
    with pytest.raises(LocalError):
        a.delete("category", cat)
    await a.sync()
    now = _iso(dt.datetime.now(dt.UTC))
    body = {
        "deviceId": "phone-a",
        "mutations": [
            {
                "mutationId": str(uuid.uuid4()),
                "entityType": "category",
                "entityId": cat,
                "op": "delete",
                "baseVersion": 1,
                "createdAt": now,
                "updatedAt": now,
                "deletedAt": now,
                "payload": None,
            }
        ],
    }
    r = await http.post("/sync/push", json=body, headers={"Authorization": f"Bearer {token(USER_A)}"})
    assert r.status_code == 422
    assert not (await store.server_state(USER_A))[("category", cat)].deleted


@pytest.mark.parametrize("first", ["archiver", "renamer"])
async def test_archive_wins_over_concurrent_rename(device, store, first):
    """A архивирует категорию офлайн, B офлайн переименовывает её ПОЗЖЕ по времени. В любом порядке
    синхронизации категория остаётся в архиве."""
    ca, cb = SkewClock(), SkewClock()
    a, b = device(USER_A, "phone-a", ca), device(USER_A, "ipad-a", cb)
    cat = a.create_category("Еда")
    await a.sync()
    await b.sync()
    a.transport.offline = b.transport.offline = True
    a.archive_category(cat)
    cb.advance(60)
    b.update("category", cat, name="Продукты")
    a.transport.offline = b.transport.offline = False
    for c in [a, b] if first == "archiver" else [b, a]:
        await c.sync()
    final = await assert_converged(store, USER_A, a, b)
    assert final[("category", cat)][0]["isArchived"] is True


# --- единая модель Expense и состав синхронизируемых данных ----------------------------------


async def test_imported_expense_is_a_regular_expense(device, store):
    """Импортированная операция после проверки пользователем сохраняется обычным расходом:
    название операции из выписки — в comment. Её правят, удаляют и синхронизируют как ручную.
    Повторный импорт тех же строк создаёт новые расходы: дубликаты не ищем."""
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a")
    cat = a.create_category("Продукты")
    statement = [("1234.50", "2026-09-03", "SUPERMARKET 0001 MOSCOW RU"), ("89.00", "2026-09-04", "METRO")]
    first = [a.create_expense(amt, cat, d, comment=desc) for amt, d, desc in statement]
    again = [a.create_expense(amt, cat, d, comment=desc) for amt, d, desc in statement]
    manual = a.create_expense("500.00", cat, "2026-09-05", "наличные")
    await a.sync()
    await b.sync()
    assert len(b.visible("expense")) == 5
    exp = b.visible("expense")[first[0]]
    assert exp == {
        "amount": "1234.50",
        "categoryId": cat,
        "date": "2026-09-03",
        "comment": "SUPERMARKET 0001 MOSCOW RU",
    }
    assert set(exp) == set(b.visible("expense")[manual])  # те же поля, что у ручного
    b.update("expense", first[0], amount="1200.00", comment="продукты на неделю")
    b.delete("expense", again[1])
    await b.sync()
    final = await assert_converged(store, USER_A, a, b)
    assert final[("expense", first[0])][0]["comment"] == "продукты на неделю"
    assert final[("expense", again[1])] == (None, True)


@pytest.mark.parametrize("entity_type", ["app_settings", "settings", "import_draft", "ui_state"])
async def test_only_expenses_and_categories_are_synced(http, store, entity_type):
    """Настройки приложения, черновик импорта и состояние UI остаются на устройстве: у сервера нет
    для них типа сущности, push отклоняется (422) и ничего не сохраняет."""
    now = _iso(dt.datetime.now(dt.UTC))
    body = {
        "deviceId": "phone-a",
        "mutations": [
            {
                "mutationId": str(uuid.uuid4()),
                "entityType": entity_type,
                "entityId": str(uuid.uuid4()),
                "op": "upsert",
                "baseVersion": 0,
                "createdAt": now,
                "updatedAt": now,
                "deletedAt": None,
                "payload": {"theme": "dark"},
            }
        ],
    }
    r = await http.post("/sync/push", json=body, headers={"Authorization": f"Bearer {token(USER_A)}"})
    assert r.status_code == 422
    assert await store.server_state(USER_A) == {}
