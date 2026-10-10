"""15 обязательных сценариев синхронизации. Идут через HTTP API (FastAPI in-process) и YDB.

Правила конфликтов — вариант B (serverVersion + baseVersion, delete wins), см. syncproto/resolve.py.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import uuid

import pytest

from client.client import SyncClient
from client.transport import FaultyTransport, HttpTransport

from .conftest import PERM_USERS, USER_A, USER_B, SkewClock, assert_converged, server_view, token

pytestmark = pytest.mark.ydb


# Создать категорию «Еда» на устройстве.
def _cat(c: SyncClient) -> str:
    return c.create_category("Еда", "🍔")


# Создать расход в категории на устройстве.
def _exp(c: SyncClient, cat: str, amount: str = "100.00", comment: str | None = None) -> str:
    return c.create_expense(amount, cat, "2026-10-01", comment)


# 1. A создаёт категорию и расход → B после sync видит их с тем же содержимым.
async def test_01_create_reaches_other_device(device, store):
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a")
    cat = _cat(a)
    e = _exp(a, cat, "250.50", "кофе")
    assert (await a.sync()).applied == 2
    await b.sync()
    assert b.visible("expense") == {
        e: {"amount": "250.50", "categoryId": cat, "date": "2026-10-01", "comment": "кофе"}
    }
    assert b.visible("category")[cat]["name"] == "Еда"
    await assert_converged(store, USER_A, a, b)


# 2. A правит сумму → B получает новую сумму.
async def test_02_edit_reaches_other_device(device, store):
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a")
    e = _exp(a, _cat(a))
    await a.sync()
    await b.sync()
    a.update("expense", e, amount="99.90")
    await a.sync()
    await b.sync()
    assert b.visible("expense")[e]["amount"] == "99.90"
    await assert_converged(store, USER_A, a, b)


# 3. A удаляет расход → у B он пропадает, а локально остаётся tombstone.
async def test_03_delete_reaches_other_device(device, store):
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a")
    e = _exp(a, _cat(a))
    await a.sync()
    await b.sync()
    a.delete("expense", e)
    await a.sync()
    await b.sync()
    assert e not in b.visible("expense")
    assert b.snapshot()[("expense", e)] == (None, True)  # tombstone дошёл
    await assert_converged(store, USER_A, a, b)


# 4. Офлайн-правки копятся в outbox и уходят после восстановления связи;
# до этого на сервер ничего не попадает.
async def test_04_offline_changes_sent_after_reconnect(device, store):
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a")
    a.transport.offline = True
    cat = _cat(a)
    ids = [_exp(a, cat, f"{i}0.00") for i in range(1, 4)]
    a.update("expense", ids[0], comment="исправлено офлайн")
    r = await a.sync()
    assert not r.ok and a.outbox_size() == 5
    assert await store.server_state(USER_A) == {}
    a.transport.offline = False
    assert (await a.sync()).ok and a.outbox_size() == 0
    await b.sync()
    assert set(b.visible("expense")) == set(ids)
    assert b.visible("expense")[ids[0]]["comment"] == "исправлено офлайн"
    await assert_converged(store, USER_A, a, b)


# 5. Тот же запрос push отправлен дважды: второй ответ — повтор (replayed), новых версий и дублей нет.
async def test_05_same_request_twice_no_duplicates(device, store, http):
    a = device(USER_A, "phone-a")
    cat = _cat(a)
    _exp(a, cat)
    rows = a.db.execute("SELECT body FROM outbox ORDER BY seq").fetchall()
    body = {"deviceId": "phone-a", "mutations": [json.loads(r[0]) for r in rows]}
    t = HttpTransport(http, token(USER_A))
    first = await t.push(copy.deepcopy(body))
    v1 = await store.last_version(USER_A)
    second = await t.push(copy.deepcopy(body))
    assert [r["status"] for r in first["results"]] == ["applied", "applied"]
    assert all(r["replayed"] for r in second["results"])
    assert [r["version"] for r in first["results"]] == [r["version"] for r in second["results"]]
    assert await store.last_version(USER_A) == v1  # повтор не назначил новых версий
    assert len(await store.server_state(USER_A)) == 2


# 6. Сервер сохранил пачку, но ответ потерян: повторная отправка безопасна, версии не меняются.
async def test_06_response_lost_after_server_commit(device, store):
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a")
    e = _exp(a, _cat(a))
    a.transport.drop_push_responses = 1
    r = await a.sync()
    assert not r.ok and a.outbox_size() == 2  # клиент не знает, что сервер всё сохранил
    v1 = await store.last_version(USER_A)
    assert ("expense", e) in await store.server_state(USER_A)
    r = await a.sync()
    assert r.ok and a.outbox_size() == 0 and r.applied == 2
    assert await store.last_version(USER_A) == v1
    assert len(await store.server_state(USER_A)) == 2
    await assert_converged(store, USER_A, a, b)


# 6b. Ответ потерян, а другое устройство тем временем изменило запись:
# повтор приносит клиенту актуальную запись.
async def test_06b_lost_response_while_other_device_edits(device, store):
    """Регрессия: повтор возвращает сохранённый результат, но запись успела измениться другим
    устройством. Без приложенной актуальной записи A навсегда остался бы со своей версией."""
    a, b = device(USER_A, "phone-a"), device(USER_A, "ipad-a", clock=SkewClock(dt.timedelta(seconds=5)))
    e = _exp(a, _cat(a))
    await a.sync()
    await b.sync()
    a.update("expense", e, amount="1.00")
    a.transport.drop_push_responses = 1
    assert not (await a.sync()).ok
    await b.sync()
    b.update("expense", e, amount="2.00")
    await b.sync()
    await a.sync()  # pull пропустит запись (есть pending), повтор push вернёт актуальную
    final = await assert_converged(store, USER_A, a, b)
    assert final[("expense", e)][0]["amount"] == "2.00"


# 7. A и B одновременно правят один расход; позже по времени правит B, но синхронизируется первым.
# Итог у всех — правка B; правка A отклонена как конфликт (LWW по записи).
async def test_07_concurrent_edit_same_expense(device, store):
    ca, cb = SkewClock(), SkewClock()
    a, b = device(USER_A, "phone-a", ca), device(USER_A, "ipad-a", cb)
    e = _exp(a, _cat(a), comment="исходный")
    await a.sync()
    await b.sync()
    a.update("expense", e, amount="111.00")
    cb.advance(2)  # B правит позже по времени
    b.update("expense", e, comment="от B")
    await b.sync()
    ra = await a.sync()  # A синхронизируется последним, но его правка старше
    final = await assert_converged(store, USER_A, a, b)
    assert final[("expense", e)][0] == {
        "amount": "100.00",
        "categoryId": final[("expense", e)][0]["categoryId"],
        "date": "2026-10-01",
        "comment": "от B",
    }
    assert ra.rejected == 1 and ra.conflicts == 1


# 7b. То же, но более поздняя правка у того, кто синхронизируется вторым: она и побеждает.
async def test_07b_concurrent_edit_later_pusher_has_newer_edit(device, store):
    ca, cb = SkewClock(), SkewClock()
    a, b = device(USER_A, "phone-a", ca), device(USER_A, "ipad-a", cb)
    e = _exp(a, _cat(a))
    await a.sync()
    await b.sync()
    b.update("expense", e, amount="1.00")
    ca.advance(2)
    a.update("expense", e, amount="2.00")
    await b.sync()
    ra = await a.sync()
    assert ra.applied == 1
    final = await assert_converged(store, USER_A, a, b)
    assert final[("expense", e)][0]["amount"] == "2.00"


# 8. A удаляет расход офлайн, B правит его офлайн ПОЗЖЕ по времени. В любом порядке синхронизации
# запись остаётся удалённой (delete wins) — никакого воскрешения.
@pytest.mark.parametrize("first", ["deleter", "editor"])
async def test_08_delete_vs_offline_edit(device, store, first):
    ca, cb = SkewClock(), SkewClock()
    a, b = device(USER_A, "phone-a", ca), device(USER_A, "ipad-a", cb)
    e = _exp(a, _cat(a))
    await a.sync()
    await b.sync()
    a.transport.offline = b.transport.offline = True
    a.delete("expense", e)
    cb.advance(60)  # правка B позже удаления — LWW по времени «воскресил» бы запись
    b.update("expense", e, amount="500.00")
    a.transport.offline = b.transport.offline = False
    order = [a, b] if first == "deleter" else [b, a]
    for c in order:
        await c.sync()
    final = await assert_converged(store, USER_A, a, b)
    assert final[("expense", e)] == (None, True), "удалённая запись воскресла"
    assert e not in a.visible("expense") and e not in b.visible("expense")


# 9. Новый телефон с cursor = 0 страницами по 7 восстанавливает всё, включая удаления.
async def test_09_new_phone_restores_everything_from_cursor_zero(device, store):
    a = device(USER_A, "phone-a")
    cat = _cat(a)
    ids = [_exp(a, cat, f"{i + 1}.00") for i in range(30)]
    for i in ids[:5]:
        a.update("expense", i, comment="изменён")
    for i in ids[5:8]:
        a.delete("expense", i)
    await a.sync()
    c = device(USER_A, "phone-new", pull_limit=7)
    assert c.cursor == 0
    r = await c.sync()
    assert r.pages == 5  # 31 запись по 7
    assert c.snapshot() == a.snapshot()
    assert len(c.visible("expense")) == 27
    await assert_converged(store, USER_A, a, c)


# 10. Первая загрузка обрывается после двух страниц; после перезапуска продолжает с сохранённого места.
# Первая загрузка устройства идёт тем же возобновляемым путём, что и resync после 410.
async def test_10_pull_interrupted_mid_pages_resumes(device, store):
    a = device(USER_A, "phone-a")
    cat = _cat(a)
    for i in range(24):
        _exp(a, cat, f"{i + 1}.00")
    await a.sync()
    c = device(USER_A, "phone-new", pull_limit=5)
    c.transport.fail_pull_after(2)
    r = await c.sync()
    assert not r.ok and r.pages == 2
    assert len(c.snapshot()) == 10 and c.resync_pending
    cursor_before = int(c._meta("resync_cursor"))
    assert cursor_before > 0
    c.close()
    c = device(USER_A, "phone-new", pull_limit=5)  # перезапуск после обрыва
    assert int(c._meta("resync_cursor")) == cursor_before
    r = await c.sync()
    assert r.ok and r.pulled == 15 and r.pages == 3  # без повторной загрузки первых 10
    assert c.snapshot() == a.snapshot()


# 11. Приложение «падает» с непустым outbox, затем теряет ответ первой отправки —
# данные всё равно доходят ровно один раз.
async def test_11_restart_with_non_empty_outbox(device, store):
    a = device(USER_A, "phone-a")
    a.transport.offline = True
    cat = _cat(a)
    ids = [_exp(a, cat) for _ in range(4)]
    a.delete("expense", ids[0])
    del a  # «падение» приложения без закрытия
    a2 = device(USER_A, "phone-a")
    assert a2.outbox_size() == 6
    a2.transport.drop_push_responses = 1  # и ещё один сбой при первой отправке
    assert not (await a2.sync()).ok
    a2.close()
    a3 = device(USER_A, "phone-a")
    assert (await a3.sync()).ok and a3.outbox_size() == 0
    state = await store.server_state(USER_A)
    assert len(state) == 5 and state[("expense", ids[0])].deleted


# 12. Пользователи изолированы даже при одинаковых UUID записей;
# userId нельзя ни передать в запросе, ни подделать токеном.
async def test_12_users_are_isolated(device, store, http):
    a = device(USER_A, "phone-a")
    e = _exp(a, _cat(a), comment="секрет A")
    await a.sync()
    b = device(USER_B, "phone-b")
    await b.sync()
    assert b.snapshot() == {}
    # B создаёт запись с тем же UUID — это другая запись в пространстве B.
    b.create_expense("1.00", str(uuid.uuid4()), "2026-10-02", "запись B", entity_id=e)
    await b.sync()
    assert (await store.server_state(USER_A))[("expense", e)].payload["comment"] == "секрет A"
    assert (await store.server_state(USER_B))[("expense", e)].payload["comment"] == "запись B"
    await assert_converged(store, USER_A, a)
    # userId нельзя передать в запросе: без токена, с чужим секретом, с полем userId.
    assert (await http.get("/sync/pull")).status_code == 401
    assert (
        await http.get("/sync/pull", params={"userId": USER_A}, headers={"Authorization": "Bearer x"})
    ).status_code == 401
    from syncproto import auth

    forged = auth.issue_test_token("x" * 48, USER_A, "money-planner-sync-proto")
    assert (await http.get("/sync/pull", headers={"Authorization": f"Bearer {forged}"})).status_code == 401
    body = {"deviceId": "phone-b", "userId": USER_A, "mutations": []}
    r = await http.post("/sync/push", json=body, headers={"Authorization": f"Bearer {token(USER_B)}"})
    assert r.status_code == 422
    # Параметр userId в query игнорируется: B видит только свои данные.
    r = await http.get(
        "/sync/pull", params={"userId": USER_A}, headers={"Authorization": f"Bearer {token(USER_B)}"}
    )
    assert {x["payload"]["comment"] for x in r.json()["records"] if x["entityType"] == "expense"} == {
        "запись B"
    }


# 13. Три конкурентные правки одной записи во всех 6 порядках прихода дают одно и то же состояние;
# с удалением среди них — запись удалена при любом порядке.
async def test_13_same_changes_different_order_converge(store, http):
    """Три конкурентные правки одной записи (одна база) в разных порядках прихода дают одно
    состояние, а повтор той же пачки — те же результаты."""
    import itertools

    eid, cat, now = str(uuid.uuid4()), str(uuid.uuid4()), dt.datetime.now(dt.UTC)

    def mut(dev: str, sec: int, comment: str | None, op: str = "upsert") -> dict:
        ts = (now + dt.timedelta(seconds=sec)).isoformat()
        return {
            "deviceId": dev,
            "mutations": [
                {
                    "mutationId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{dev}-{sec}-{op}")),
                    "entityType": "expense",
                    "entityId": eid,
                    "op": op,
                    "baseVersion": 1,
                    "createdAt": now.isoformat(),
                    "updatedAt": ts,
                    "deletedAt": ts if op == "delete" else None,
                    "payload": None
                    if op == "delete"
                    else {"amount": "10.00", "categoryId": cat, "date": "2026-10-01", "comment": comment},
                }
            ],
        }

    create = mut("seed", 0, "seed")
    create["mutations"][0]["baseVersion"] = 0
    edits = [mut("dev-x", 1, "X"), mut("dev-y", 3, "Y"), mut("dev-z", 2, "Z")]
    finals = []
    for user, order in zip(PERM_USERS, itertools.permutations(edits), strict=False):
        t = HttpTransport(http, token(user))
        await t.push(create)
        for m in order:
            await t.push(m)
        finals.append(server_view(await store.server_state(user)))
    assert len(finals) == 6 and all(f == finals[0] for f in finals)
    assert finals[0][("expense", eid)][0]["comment"] == "Y"  # самая поздняя из конкурентных
    # С удалением: удаление побеждает в любом порядке.
    finals = []
    with_delete = [*edits[:2], mut("dev-d", 0, None, op="delete")]
    for user, order in zip(
        PERM_USERS[6:] + [USER_A, USER_B], list(itertools.permutations(with_delete))[:4], strict=False
    ):
        t = HttpTransport(http, token(user))
        await t.push(create)
        for m in order:
            await t.push(m)
        finals.append(server_view(await store.server_state(user)))
    assert all(f[("expense", eid)] == (None, True) for f in finals)


# 14. 8 устройств одного пользователя отправляют push одновременно: версии уникальны и без пропусков,
# одновременная правка одной записи с двух устройств сходится к одному состоянию.
async def test_14_concurrent_pushes_same_user(device, store, http):
    devices = [device(USER_A, f"dev-{i}") for i in range(8)]
    cat = _cat(devices[0])
    for d in devices:
        for j in range(15):
            _exp(d, cat, f"{j + 1}.00")
    reports = await asyncio.gather(*(d.push_all() for d in devices))
    assert sum(r.applied for r in reports) == 8 * 15 + 1
    state = await store.server_state(USER_A)
    versions = sorted(r.version for r in state.values())
    assert len(versions) == len(set(versions)) == 121
    assert await store.last_version(USER_A) == versions[-1] == 121  # без пропусков: каждое применение +1
    # Конкурентная правка одной записи с двух устройств одновременно: ровно один результат.
    shared = devices[0].create_expense("5.00", cat, "2026-10-03")
    await devices[0].sync()
    await devices[1].sync()
    devices[0].update("expense", shared, amount="6.00")
    devices[1].update("expense", shared, amount="7.00")
    await asyncio.gather(devices[0].push_all(), devices[1].push_all())
    await assert_converged(store, USER_A, *devices)


# 15. Первичная синхронизация 1200 записей: выгрузка 3 пачками по 500, загрузка 3 страницами.
async def test_15_large_initial_sync_in_batches(device, store):
    a = device(USER_A, "phone-a", push_batch=500)
    cat = _cat(a)
    for i in range(1199):
        _exp(a, cat, f"{i % 900 + 1}.00", f"синтетика {i}")
    r = await a.push_all()
    assert r.push_requests == 3 and r.applied == 1200
    c = device(USER_A, "phone-new", pull_limit=500)
    r = await c.pull_all()
    assert r.pages == 3 and r.pulled == 1200
    assert c.snapshot() == a.snapshot()
    assert len(await store.server_state(USER_A)) == 1200


# Курсор старше горизонта очистки tombstones — 410; полная перезагрузка с 0 разрешена.
async def test_resync_required_after_tombstone_horizon(device, store):
    a = device(USER_A, "phone-a")
    _exp(a, _cat(a))
    await a.sync()
    await store.purge_tombstones(USER_A, 100)
    t = FaultyTransport(HttpTransport(a.transport.inner.client, token(USER_A)))
    from client.transport import ResyncRequiredError

    with pytest.raises(ResyncRequiredError):
        await t.pull(1, 10)
    assert (await t.pull(0, 10))["records"]  # полная перезагрузка с 0 разрешена
