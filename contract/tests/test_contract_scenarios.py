"""Сценарии контракта на эталонной модели: авторизация, повторный вход Apple, удаление, импорт, sync.

Каждый ответ модели проверяется `conforms()` против contract/openapi.yaml. Эти же сценарии
на этапах E2–E7 запускаются против приложения.
"""

import copy
import uuid

import pytest
import refmodel as rm

OTHER = "00000000-0000-4000-8000-000000000099"
PRODUCTS = "00000000-0000-4000-8000-000000000001"
CAFE = "00000000-0000-4000-8000-000000000002"
CATEGORIES = [
    {"id": PRODUCTS, "name": "Продукты"},
    {"id": CAFE, "name": "Кафе"},
    {"id": OTHER, "name": "Другое"},
]
STATEMENT = b"2026-09-18;1250.00;PYATEROCHKA\n2026-09-19;399.00;COFFEE POINT\n2026-09-20;99.00;KIOSK"


@pytest.fixture
def server(spec):
    return rm.RefServer(spec)


@pytest.fixture
def call(spec, server):
    """call(method, path, fn, *args) — вызов модели с проверкой ответа по контракту."""

    def _call(method, path, fn, *args, **kwargs):
        resp = fn(*args, **kwargs)
        rm.conforms(spec, method, path, resp)
        return resp

    return _call


def new_guest(server, call):
    return call("post", "/v1/auth/guest", server.guest, str(uuid.uuid4())).body


def sign_in(server, call, guest, ident):
    return call("post", "/v1/auth/apple", server.apple, guest["accessToken"], ident.request())


# --- авторизация -----------------------------------------------------------------------------
class TestAuth:
    def test_guest_replay_and_reuse(self, server, call):
        key = str(uuid.uuid4())
        first = call("post", "/v1/auth/guest", server.guest, key)
        again = call("post", "/v1/auth/guest", server.guest, key)
        assert again.status == 201 and again.headers["Idempotent-Replayed"] == "true"
        assert (again.body["deviceId"], again.body["refreshToken"]) == (
            first.body["deviceId"],
            first.body["refreshToken"],
        )
        call("post", "/v1/auth/refresh", server.refresh_session, first.body["refreshToken"])
        assert call("post", "/v1/auth/guest", server.guest, key).code == "idempotency_key_reused"
        assert call("post", "/v1/auth/guest", server.guest, "not-a-uuid").code == "invalid_request"

    def test_refresh_lost_response_within_grace(self, server, call):
        g = new_guest(server, call)
        lost = call("post", "/v1/auth/refresh", server.refresh_session, g["refreshToken"])
        server.tick(rm.GRACE_S - 1)
        retry = call("post", "/v1/auth/refresh", server.refresh_session, g["refreshToken"])
        assert retry.status == 200 and retry.body == lost.body

    def test_refresh_lost_response_beyond_grace_guest(self, server, call):
        g = new_guest(server, call)
        lost = call("post", "/v1/auth/refresh", server.refresh_session, g["refreshToken"])
        server.tick(rm.GRACE_S + 1)
        retry = call("post", "/v1/auth/refresh", server.refresh_session, g["refreshToken"])
        assert retry.status == 401 and retry.code == "refresh_token_invalid"
        # reuse detection отзывает всю family, включая пару из потерянного ответа
        stolen = call("post", "/v1/auth/refresh", server.refresh_session, lost.body["refreshToken"])
        assert stolen.code == "refresh_token_invalid"
        # гость создаёт новую гостевую запись: новое устройство, счётчик импортов с нуля
        g2 = new_guest(server, call)
        assert g2["deviceId"] != g["deviceId"]
        assert call("get", "/v1/me", server.me, g2["accessToken"]).body["features"]["imports"]["used"] == 0

    def test_refresh_lost_response_beyond_grace_account(self, server, call):
        g = new_guest(server, call)
        acc = sign_in(server, call, g, rm.AppleIdentity.new("apple-sub-1", server.now)).body
        call("post", "/v1/auth/refresh", server.refresh_session, acc["refreshToken"])
        server.tick(rm.GRACE_S + 1)
        assert call("post", "/v1/auth/refresh", server.refresh_session, acc["refreshToken"]).code == (
            "refresh_token_invalid"
        )
        # reauth_required: повторный вход тем же Apple ID → тот же аккаунт, sync продолжается
        again = sign_in(server, call, g, rm.AppleIdentity.new("apple-sub-1", server.now)).body
        assert again["accountId"] == acc["accountId"]
        assert rm.client_after_sign_in(acc["accountId"], True, again) == "continue_sync"

    def test_client_retry_budget_fits_grace(self, config_params):
        client = {p["name"]: p["value"] for p in config_params["client"]}
        proto = {p["name"]: p["value"] for p in config_params["protocol"]}
        assert client["retry_budget_s"] < proto["REFRESH_GRACE_S"]

    def test_401_codes(self, server, call):
        g = new_guest(server, call)
        assert call("get", "/v1/me", server.me, "garbage").code == "access_token_invalid"
        server.tick(rm.ACCESS_TTL_S + 1)
        assert call("get", "/v1/me", server.me, g["accessToken"]).code == "token_expired"
        fresh = call("post", "/v1/auth/refresh", server.refresh_session, g["refreshToken"]).body
        assert call("get", "/v1/me", server.me, fresh["accessToken"]).status == 200

    def test_guest_cannot_delete_account(self, server, call):
        g = new_guest(server, call)
        assert (
            call("delete", "/v1/account", server.delete_account, g["accessToken"]).code == "account_required"
        )

    def test_consent_revoked_then_same_apple_id(self, server, call):
        g = new_guest(server, call)
        acc = sign_in(server, call, g, rm.AppleIdentity.new("sub-c", server.now)).body
        server.consent_revoked("sub-c")
        assert call("get", "/v1/me", server.me, acc["accessToken"]).code == "session_revoked"
        assert call("post", "/v1/auth/refresh", server.refresh_session, acc["refreshToken"]).code == (
            "session_revoked"
        )
        again = sign_in(server, call, g, rm.AppleIdentity.new("sub-c", server.now)).body
        assert again["accountId"] == acc["accountId"]
        assert call("get", "/v1/me", server.me, again["accessToken"]).status == 200

    def test_logout_is_idempotent(self, server, call):
        g = new_guest(server, call)
        acc = sign_in(server, call, g, rm.AppleIdentity.new("sub-l", server.now)).body
        for _ in range(2):
            assert call("post", "/v1/auth/logout", server.logout, acc["refreshToken"]).status == 204
        assert call("post", "/v1/auth/logout", server.logout, "rt_unknown_token_value").status == 204
        assert call("post", "/v1/auth/refresh", server.refresh_session, acc["refreshToken"]).code == (
            "refresh_token_invalid"
        )


# --- повторный вход Apple после потерянного ответа ----------------------------------------------
class TestAppleLostResponse:
    def test_recovery_with_new_authorization(self, server, call):
        g = new_guest(server, call)
        tok = g["accessToken"]
        # гостевой импорт до входа — будет атрибутирован аккаунту ровно один раз
        r = call("post", "/v1/imports/parse", server.parse, tok, str(uuid.uuid4()), STATEMENT, CATEGORIES)
        call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, r.body["importId"])

        first = rm.AppleIdentity.new("sub-lost", server.now)
        lost = sign_in(server, call, g, first)  # обработан сервером, ответ до клиента не дошёл
        assert lost.status == 200

        retry = sign_in(server, call, g, first)
        assert retry.status == 400 and retry.code == "apple_token_invalid"
        assert retry.body["error"]["retryable"] is False

        second = rm.AppleIdentity.new("sub-lost", server.now)
        ok = sign_in(server, call, g, second)
        assert ok.status == 200
        assert ok.body["accountId"] == lost.body["accountId"]  # дубль аккаунта не создан
        assert len(server.accounts) == 1 and list(server.identities.values()) == [ok.body["accountId"]]
        assert server.apple_refresh_tokens[ok.body["accountId"]] == "apple_rt_for_" + second.code
        me = call("get", "/v1/me", server.me, ok.body["accessToken"]).body
        assert me["features"]["imports"]["used"] == 1  # атрибуция не повторилась
        assert ok.body["sync"] == lost.body["sync"]  # клиент ничего не отправлял: облако не изменилось

    def test_client_continues_without_data_loss(self, server, call):
        """reauth_required: потерянный ответ + новая авторизация → продолжение sync, outbox цел."""
        g = new_guest(server, call)
        acc = sign_in(server, call, g, rm.AppleIdentity.new("sub-r", server.now)).body
        outbox = [{"mutationId": str(uuid.uuid4())} for _ in range(3)]
        saved = copy.deepcopy(outbox)
        server.consent_revoked("sub-r")
        sign_in(server, call, g, rm.AppleIdentity.new("sub-r", server.now))  # ответ потерян
        ok = sign_in(server, call, g, rm.AppleIdentity.new("sub-r", server.now)).body
        assert rm.client_after_sign_in(acc["accountId"], True, ok) == "continue_sync"
        assert outbox == saved

    def test_first_sign_in_flow_after_lost_response(self, server, call):
        g = new_guest(server, call)
        sign_in(server, call, g, rm.AppleIdentity.new("sub-f", server.now))  # потерян
        ok = sign_in(server, call, g, rm.AppleIdentity.new("sub-f", server.now)).body
        # клиент ещё не выбирал «Объединить / Заменить» — выбор происходит после успешного ответа
        assert rm.client_after_sign_in(None, True, ok) == "upload_local"
        assert rm.client_after_sign_in(None, False, ok) == "full_download"

    def test_two_devices_same_apple_id_share_account(self, server, call):
        a = sign_in(server, call, new_guest(server, call), rm.AppleIdentity.new("sub-2", server.now)).body
        b = sign_in(server, call, new_guest(server, call), rm.AppleIdentity.new("sub-2", server.now)).body
        assert a["accountId"] == b["accountId"] and a["deviceId"] != b["deviceId"]

    @pytest.mark.parametrize("broken", ["aud", "exp", "nonce"])
    def test_invalid_apple_credentials(self, server, call, broken):
        g = new_guest(server, call)
        ident = rm.AppleIdentity.new("sub-x", server.now)
        req = ident.request()
        if broken == "aud":
            req["identityToken"]["aud"] = "com.other.app"
        elif broken == "exp":
            req["identityToken"]["exp"] = server.now - 1
        else:
            req["nonce"] = "another-nonce-value"
        assert (
            call("post", "/v1/auth/apple", server.apple, g["accessToken"], req).code == "apple_token_invalid"
        )

    def test_sign_in_requires_guest_token(self, server, call):
        ident = rm.AppleIdentity.new("sub-y", server.now)
        assert call("post", "/v1/auth/apple", server.apple, "garbage", ident.request()).code == (
            "access_token_invalid"
        )


# --- удаление аккаунта -------------------------------------------------------------------------
class TestDeletion:
    def _two_devices(self, server, call, sub="sub-d"):
        g1, g2 = new_guest(server, call), new_guest(server, call)
        a1 = sign_in(server, call, g1, rm.AppleIdentity.new(sub, server.now)).body
        a2 = sign_in(server, call, g2, rm.AppleIdentity.new(sub, server.now)).body
        return g1, g2, a1, a2

    def test_delete_flow(self, server, call):
        g1, g2, a1, a2 = self._two_devices(server, call)
        d = call("delete", "/v1/account", server.delete_account, a1["accessToken"])
        assert d.status == 202
        again = call("delete", "/v1/account", server.delete_account, a1["accessToken"])
        assert again.body["deletionId"] == d.body["deletionId"]
        # второе устройство: account_deleted и на запросах, и на refresh
        assert call("get", "/v1/me", server.me, a2["accessToken"]).code == "account_deleted"
        assert call("post", "/v1/auth/refresh", server.refresh_session, a2["refreshToken"]).code == (
            "account_deleted"
        )
        did = d.body["deletionId"]
        st = call("get", "/v1/account/deletions/{deletionId}", server.deletion_status, g1["accessToken"], did)
        assert st.status == 200
        assert (
            call(
                "get", "/v1/account/deletions/{deletionId}", server.deletion_status, g2["accessToken"], did
            ).code
            == "deletion_not_found"
        )
        server.run_deletions()
        done = call(
            "get", "/v1/account/deletions/{deletionId}", server.deletion_status, g1["accessToken"], did
        )
        assert done.body["state"] == "done"
        # после полного удаления refresh аккаунта — по-прежнему account_deleted
        assert call("post", "/v1/auth/refresh", server.refresh_session, a2["refreshToken"]).code == (
            "account_deleted"
        )

    def test_sign_in_during_deletion_then_new_account(self, server, call):
        g1, g2, a1, _ = self._two_devices(server, call, sub="sub-del")
        call("delete", "/v1/account", server.delete_account, a1["accessToken"])
        ident = rm.AppleIdentity.new("sub-del", server.now)
        busy = sign_in(server, call, g2, ident)
        assert busy.status == 409 and busy.code == "account_deletion_in_progress"
        assert int(busy.headers["Retry-After"]) > 0 and busy.body["error"]["retryable"] is True
        assert server.accounts[a1["accountId"]]["state"] == "deleting"  # удаление не отменено
        server.run_deletions()
        # тот же запрос: nonce и код не были израсходованы на 409
        fresh = sign_in(server, call, g2, ident)
        assert fresh.status == 200
        assert fresh.body["accountId"] != a1["accountId"]  # новый аккаунт
        assert fresh.body["sync"] == {"lastVersion": 0, "hasData": False}  # данные не восстановлены
        me = call("get", "/v1/me", server.me, fresh.body["accessToken"]).body
        assert me["features"]["imports"]["used"] == 0

    def test_revocation_skipped_without_apple_token(self, server, call):
        g, _, a1, _ = self._two_devices(server, call, sub="sub-s")
        server.apple_refresh_tokens.clear()
        did = call("delete", "/v1/account", server.delete_account, a1["accessToken"]).body["deletionId"]
        st = call("get", "/v1/account/deletions/{deletionId}", server.deletion_status, g["accessToken"], did)
        assert st.body["appleTokenRevocation"] == "skipped_no_token"


class TestDeletionJob:
    """Порядок шагов, прерывание, аренда, повторы и восстановление фонового задания (план §2.5)."""

    def _start(self, server, call, sub, rows=2500):
        g = new_guest(server, call)
        a = sign_in(server, call, g, rm.AppleIdentity.new(sub, server.now)).body
        server.sync_rows[a["accountId"]] = rows
        did = call("delete", "/v1/account", server.delete_account, a["accessToken"]).body["deletionId"]
        return g, a, did

    def test_identities_is_the_last_step(self):
        assert rm.DELETION_STEPS[-1] == "identities"
        assert rm.DELETION_STEPS.index("users") < rm.DELETION_STEPS.index("identities")

    def test_interrupted_job_resumes_from_cursor(self, server, call):
        g, a, did = self._start(server, call, "sub-int")
        server.run_deletions(max_batches=2)  # экземпляр «упал» после 2 пачек sync_records
        job = server.deletions[did]
        assert (rm.DELETION_STEPS[job["step"]], job["cursor"]) == ("sync_records", 2000)
        assert job["state"] == "deleting" and server.sync_rows[a["accountId"]] == 500
        # до завершения вход тем же Apple ID — 409, повтор DELETE — тот же id
        ident = rm.AppleIdentity.new("sub-int", server.now)
        assert sign_in(server, call, g, ident).code == "account_deletion_in_progress"
        assert (
            call("delete", "/v1/account", server.delete_account, a["accessToken"]).body["deletionId"] == did
        )
        server.run_deletions()  # аренда ещё действует — другой экземпляр не берёт задание
        assert server.deletions[did]["batches"] == 2
        server.tick(rm.DELETION_LEASE_S + 1)
        server.run_deletions()
        job = server.deletions[did]
        # продолжение с курсора: всего 3 пачки sync_records + по одной на остальные шаги
        assert job["state"] == "done" and job["batches"] == 3 + len(rm.DELETION_STEPS) - 1
        assert server.sync_rows[a["accountId"]] == 0
        assert a["accountId"] not in server.accounts and a["accountId"] not in server.identities.values()
        assert a["accountId"] not in server.apple_refresh_tokens
        fresh = sign_in(server, call, g, rm.AppleIdentity.new("sub-int", server.now)).body
        assert fresh["accountId"] != a["accountId"]

    def test_interrupted_before_identities_still_409(self, server, call):
        g, a, did = self._start(server, call, "sub-late", rows=10)
        server.run_deletions(max_batches=len(rm.DELETION_STEPS) - 1)  # всё, кроме identities
        assert a["accountId"] not in server.accounts  # users уже удалён
        assert server.identities["sub-late"] == a["accountId"]
        busy = sign_in(server, call, g, rm.AppleIdentity.new("sub-late", server.now))
        assert busy.code == "account_deletion_in_progress"
        assert call("get", "/v1/me", server.me, a["accessToken"]).code == "account_deleted"
        server.tick(rm.DELETION_LEASE_S + 1)
        server.run_deletions()
        assert server.deletions[did]["state"] == "done"

    def test_apple_revoke_failure_retries_with_backoff(self, server, call):
        g, a, did = self._start(server, call, "sub-rev", rows=10)
        server.apple_revoke = lambda token: 503
        server.run_deletions()
        job = server.deletions[did]
        assert job["state"] == "failed_retrying" and job["revocation"] == "pending"
        assert job["step"] == len(rm.DELETION_STEPS) - 1  # данные удалены, identities ждёт отзыва
        st = call("get", "/v1/account/deletions/{deletionId}", server.deletion_status, g["accessToken"], did)
        assert st.body["state"] == "failed_retrying"
        assert sign_in(server, call, g, rm.AppleIdentity.new("sub-rev", server.now)).code == (
            "account_deletion_in_progress"
        )
        server.apple_revoke = lambda token: 200
        server.run_deletions()  # backoff ещё не истёк
        assert server.deletions[did]["state"] == "failed_retrying"
        server.tick(rm.DELETION_BACKOFF_MAX_S)
        server.run_deletions()
        tok = call("post", "/v1/auth/refresh", server.refresh_session, g["refreshToken"]).body["accessToken"]
        st = call("get", "/v1/account/deletions/{deletionId}", server.deletion_status, tok, did)
        assert (st.body["state"], st.body["appleTokenRevocation"]) == ("done", "done")

    def test_fresh_authorization_code_used_without_stored_token(self, server, call):
        g = new_guest(server, call)
        a = sign_in(server, call, g, rm.AppleIdentity.new("sub-code", server.now)).body
        server.apple_refresh_tokens.clear()
        did = call("delete", "/v1/account", server.delete_account, a["accessToken"], "fresh-code").body[
            "deletionId"
        ]
        revoked = []
        server.apple_revoke = lambda token: revoked.append(token) or 200
        server.run_deletions()
        assert revoked == ["apple_rt_for_fresh-code"]
        assert server.deletions[did]["revocation"] == "done"

    def test_rerun_after_done_is_noop(self, server, call):
        _, _, did = self._start(server, call, "sub-noop", rows=1)
        server.run_deletions()
        before = dict(server.deletions[did])
        server.tick(rm.DELETION_LEASE_S + 1)
        server.run_deletions()
        assert server.deletions[did] == before


# --- импорт: идемпотентность и черновик --------------------------------------------------------
class TestImportIdempotency:
    @pytest.fixture
    def guest(self, server, call):
        return new_guest(server, call)

    def parse(self, server, call, token, key, file=STATEMENT, **kw):
        return call("post", "/v1/imports/parse", server.parse, token, key, file, CATEGORIES, **kw)

    def used(self, server, call, token):
        return call("get", "/v1/me", server.me, token).body["features"]["imports"]["used"]

    def test_same_key_and_file_keeps_import_id_and_counter(self, server, call, guest):
        tok, key = guest["accessToken"], str(uuid.uuid4())
        first = self.parse(server, call, tok, key).body
        lost_retry = self.parse(server, call, tok, key).body
        assert lost_retry["importId"] == first["importId"]
        assert lost_retry["parserVersion"] == first["parserVersion"]
        strip = [{k: o[k] for k in ("date", "amount", "comment")} for o in first["operations"]]
        assert strip == [{k: o[k] for k in ("date", "amount", "comment")} for o in lost_retry["operations"]]
        assert self.used(server, call, tok) == 0  # без подтверждения показа не засчитан
        call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, first["importId"])
        after = self.parse(server, call, tok, key).body
        assert after["importId"] == first["importId"] and after["confirmed"] is True
        assert self.used(server, call, tok) == 1

    def test_repeat_displayed_does_not_consume(self, server, call, guest):
        tok = guest["accessToken"]
        iid = self.parse(server, call, tok, str(uuid.uuid4())).body["importId"]
        r1 = call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, iid)
        r2 = call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, iid)
        assert r1.body == r2.body and r2.body["imports"]["used"] == 1

    def test_draft_not_overwritten_by_retry_with_different_ai(self, server, call, guest):
        tok, key = guest["accessToken"], str(uuid.uuid4())
        # LLM отвечает по-разному на разных попытках
        server.ai_answer = lambda k, attempt: (CAFE, "high") if attempt == 1 else (PRODUCTS, "medium")
        client = rm.RefClientDrafts()
        first = self.parse(server, call, tok, key).body
        draft = client.on_parse_response(first)
        client.on_displayed(first["importId"])
        draft["operations"][1]["categoryId"] = OTHER  # правка пользователя
        del draft["operations"][2]  # удалённая строка
        snapshot = copy.deepcopy(draft["operations"])

        retry = self.parse(server, call, tok, key).body  # потерянный ответ → повтор с тем же ключом
        assert retry["importId"] == first["importId"]
        assert [o["categoryId"] for o in retry["operations"]] != [
            o["categoryId"] for o in first["operations"]
        ]
        merged = client.on_parse_response(retry)
        assert merged["operations"] == snapshot  # черновик не перезаписан
        assert client.confirm_queue == [first["importId"]]

    def test_confirmed_retry_recreates_deleted_draft_without_confirmation(self, server, call, guest):
        tok, key = guest["accessToken"], str(uuid.uuid4())
        client = rm.RefClientDrafts()
        first = self.parse(server, call, tok, key).body
        client.on_parse_response(first)
        client.on_displayed(first["importId"])
        call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, client.confirm_queue.pop())
        del client.drafts[first["importId"]]
        retry = self.parse(server, call, tok, key).body
        assert retry["confirmed"] is True
        client.on_parse_response(retry)
        client.on_displayed(retry["importId"])
        assert client.confirm_queue == [] and self.used(server, call, tok) == 1

    def test_other_file_same_key_and_parse_errors(self, server, call, guest):
        tok, key = guest["accessToken"], str(uuid.uuid4())
        self.parse(server, call, tok, key)
        assert self.parse(server, call, tok, key, file=STATEMENT + b"\n2026-09-21;1.00;X").code == (
            "idempotency_key_reused"
        )
        bad_key = str(uuid.uuid4())
        assert self.parse(server, call, tok, bad_key, file=b"BAD").code == "unknown_bank"
        assert (guest["deviceId"], bad_key) not in server.deliveries  # ошибка разбора попытку не расходует
        assert self.parse(server, call, tok, str(uuid.uuid4()), file=b"x" * (rm.MAX_FILE_BYTES + 1)).code == (
            "file_too_large"
        )
        assert self.parse(server, call, tok, None).code == "invalid_request"

    def test_in_progress_and_unconfirmed_limit(self, server, call, guest):
        tok, key = guest["accessToken"], str(uuid.uuid4())
        assert self.parse(server, call, tok, key).status == 200
        server.parse_in_progress.add((guest["deviceId"], key))
        busy = self.parse(server, call, tok, key)
        assert busy.code == "idempotency_in_progress" and "Retry-After" in busy.headers
        server.parse_in_progress.clear()
        for _ in range(rm.UNCONFIRMED_MAX - 1):
            assert self.parse(server, call, tok, str(uuid.uuid4())).status == 200
        limited = self.parse(server, call, tok, str(uuid.uuid4()))
        assert limited.code == "unconfirmed_imports_limit" and int(limited.headers["Retry-After"]) > 0
        assert self.parse(server, call, tok, key).status == 200  # повтор ключа в лимит не входит
        server.tick(rm.DAY + 1)
        tok = call("post", "/v1/auth/refresh", server.refresh_session, guest["refreshToken"]).body[
            "accessToken"
        ]
        assert self.parse(server, call, tok, str(uuid.uuid4())).status == 200  # окно 24 ч прошло

    def test_fourth_import_not_blocked_and_counter_persists(self, server, call, guest):
        tok = guest["accessToken"]
        for _ in range(4):
            iid = self.parse(server, call, tok, str(uuid.uuid4())).body["importId"]
            call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, iid)
        usage = call("get", "/v1/me", server.me, tok).body["features"]["imports"]
        assert usage == {"used": 4, "limit": 3, "state": "quota_exhausted", "enforced": False}
        server.tick(31 * rm.DAY)
        tok = call("post", "/v1/auth/refresh", server.refresh_session, guest["refreshToken"]).body[
            "accessToken"
        ]
        assert self.used(server, call, tok) == 4  # не обнуляется через 7 и 30 дней

    def test_displayed_foreign_device_and_expired(self, server, call, guest):
        tok = guest["accessToken"]
        iid = self.parse(server, call, tok, str(uuid.uuid4())).body["importId"]
        other = new_guest(server, call)
        assert call(
            "post", "/v1/imports/{importId}/displayed", server.displayed, other["accessToken"], iid
        ).code == ("import_not_found")
        assert call(
            "post", "/v1/imports/{importId}/displayed", server.displayed, tok, rm.uuid7(server.now)
        ).code == ("import_not_found")
        server.tick(rm.DELIVERY_TTL_S + 1)
        tok = call("post", "/v1/auth/refresh", server.refresh_session, guest["refreshToken"]).body[
            "accessToken"
        ]
        assert call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, iid).code == (
            "delivery_expired"
        )
        assert self.used(server, call, tok) == 0

    def test_expired_key_starts_new_import(self, server, call, guest):
        key = str(uuid.uuid4())
        first = self.parse(server, call, guest["accessToken"], key).body
        server.tick(rm.DELIVERY_TTL_S + 1)
        tok = call("post", "/v1/auth/refresh", server.refresh_session, guest["refreshToken"]).body[
            "accessToken"
        ]
        assert self.parse(server, call, tok, key).body["importId"] != first["importId"]

    def test_category_status_semantics(self, server, call, guest):
        tok = guest["accessToken"]
        hints = [{"merchantKey": "pyaterochka", "categoryId": PRODUCTS}]
        server.ai_answer = lambda k, a: {"coffee point": (CAFE, "medium"), "kiosk": (OTHER, "low")}.get(k)
        ops = self.parse(server, call, tok, str(uuid.uuid4()), hints=hints).body["operations"]
        assert [(o["categoryId"], o["categoryStatus"]) for o in ops] == [
            (PRODUCTS, "assigned"),
            (CAFE, "suggested"),
            (None, "unassigned"),
        ]
        server.ai_enabled = False
        calls = server.ai_calls
        ops = self.parse(server, call, tok, str(uuid.uuid4()), hints=hints).body["operations"]
        assert [o["categoryStatus"] for o in ops] == ["assigned", "unassigned", "unassigned"]
        assert server.ai_calls == calls

    def test_guest_import_attributed_once(self, server, call, guest):
        tok = guest["accessToken"]
        iid = self.parse(server, call, tok, str(uuid.uuid4())).body["importId"]
        call("post", "/v1/imports/{importId}/displayed", server.displayed, tok, iid)
        a = sign_in(server, call, guest, rm.AppleIdentity.new("sub-i", server.now)).body
        assert self.used(server, call, a["accessToken"]) == 1
        g2 = new_guest(server, call)
        b = sign_in(server, call, g2, rm.AppleIdentity.new("sub-j", server.now)).body
        assert self.used(server, call, b["accessToken"]) == 0
        # то же устройство входит в другой аккаунт: импорт уже атрибутирован первому
        call("post", "/v1/auth/logout", server.logout, a["refreshToken"])
        c = sign_in(server, call, guest, rm.AppleIdentity.new("sub-j", server.now)).body
        assert self.used(server, call, c["accessToken"]) == 0
        assert self.used(server, call, tok) == 1  # счётчик устройства сохранён


# --- sync: граница валидации и 410 -------------------------------------------------------------
TODAY = "2026-10-11"


def _m(entity_type, payload, op="upsert", **kw):
    m = {
        "mutationId": str(uuid.uuid4()),
        "entityType": entity_type,
        "entityId": str(uuid.uuid4()),
        "op": op,
        "baseVersion": 0,
        "createdAt": "2026-10-11T09:00:00+03:00",
        "updatedAt": "2026-10-11T09:00:00+03:00",
        "deletedAt": "2026-10-11T09:00:00+03:00" if op == "delete" else None,
        "schemaVersion": 1,
        "payload": payload,
    }
    m.update(kw)
    return m


def _day(date, marked=True):
    return _m("day_mark", {"date": date, "isMarked": marked}, entityId=str(uuid.uuid5(rm.NS_DAY_MARK, date)))


EXPENSE = {"amount": "250.50", "categoryId": OTHER, "date": TODAY, "comment": "Кофе"}
USER_CAT = {"kind": "user", "name": "Кофе", "emoji": "☕", "colorIndex": 4, "isArchived": False}


class TestSyncContract:
    def push(self, spec, registry, mutations, size=1000, **kw):
        return rm.classify_push(
            spec, {"deviceId": "dev-1", "mutations": mutations}, size, registry, TODAY, **kw
        )

    @pytest.mark.parametrize(
        "mutation,detail",
        [
            (_m("expense", EXPENSE), None),
            (_m("expense", None, op="delete"), None),
            (_m("category", USER_CAT), None),
            (_m("category", {"kind": "system", "isArchived": True}, entityId=PRODUCTS), None),
            (_day("2026-10-12"), None),
            (_m("expense", {**EXPENSE, "comment": "a" * 201}), "comment_too_long"),
            (_m("expense", {**EXPENSE, "amount": "0.00"}), "payload_invalid"),
            (_m("category", None, op="delete", entityId=PRODUCTS), "op_not_allowed"),
            (_m("day_mark", None, op="delete"), "op_not_allowed"),
            (_m("category", {"kind": "system", "isArchived": True}), "system_category_unknown"),
            (_m("category", USER_CAT, entityId=CAFE), "category_kind_mismatch"),
            (
                _m("category", {"kind": "system", "isArchived": True}, entityId=OTHER),
                "fallback_category_not_archivable",
            ),
            (_m("category", {"kind": "system", "isArchived": False}, entityId=OTHER), None),
            (_m("day_mark", {"date": TODAY, "isMarked": True}), "day_mark_id_mismatch"),
            (_day("2026-10-13"), "date_in_future"),
        ],
    )
    def test_per_mutation_verdicts(self, spec, registry, mutation, detail):
        ok = _m("expense", EXPENSE)
        verdicts = self.push(spec, registry, [mutation, ok])
        assert isinstance(verdicts, list) and verdicts[1] is None  # остальные мутации не блокируются
        got = verdicts[0]["details"][0]["code"] if verdicts[0] else None
        assert got == detail
        results = [
            v
            or {
                "mutationId": m["mutationId"],
                "status": "applied",
                "conflict": False,
                "version": 1,
                "replayed": False,
            }
            for v, m in zip(verdicts, [mutation, ok], strict=True)
        ]
        rm.conforms(spec, "post", "/v1/sync/push", rm.Resp(200, {"results": results}, {"X-Request-Id": "r"}))

    def test_category_kind_change(self, spec, registry):
        m = _m("category", USER_CAT)
        v = self.push(spec, registry, [m], existing_kinds={m["entityId"]: "system"})
        assert v[0]["details"][0]["code"] == "category_kind_change"

    @pytest.mark.parametrize(
        "mutations,size,code",
        [
            ([_m("expense", EXPENSE)] * 501, 1000, "payload_too_large"),
            ([_m("expense", EXPENSE)], 1_048_577, "payload_too_large"),
            ([_m("app_settings", {"theme": "dark"})], 1000, "validation_failed"),
            ([_m("expense", EXPENSE, mutationId="m-1")], 1000, "validation_failed"),
            ([_m("expense", EXPENSE, schemaVersion=2)], 1000, "upgrade_required"),
            ([], 1000, "validation_failed"),
        ],
    )
    def test_request_level_errors(self, spec, registry, mutations, size, code):
        r = self.push(spec, registry, mutations, size=size)
        assert isinstance(r, rm.Resp) and r.code == code
        rm.conforms(spec, "post", "/v1/sync/push", r)

    def test_unknown_entity_type_detail(self, spec, registry):
        r = self.push(spec, registry, [_m("expense", EXPENSE), _m("app_settings", {})])
        assert r.body["error"]["details"] == [
            {"code": "unknown_entity_type", "field": "entityType", "index": 1}
        ]

    @pytest.mark.parametrize(
        "cursor,client_horizon,tombstone_horizon,expected",
        [
            (0, 0, 10, False),  # первая загрузка
            (5, 0, 10, True),  # курсор за горизонтом, горизонт сдвинулся
            (5, 10, 10, False),  # постраничная загрузка с нуля после очистки: горизонт уже известен
            (15, 0, 10, False),  # курсор после горизонта
            (10, 0, 10, False),
            (9, 9, 10, True),
        ],
    )
    def test_resync_condition(self, spec, cursor, client_horizon, tombstone_horizon, expected):
        assert rm.needs_resync(cursor, client_horizon, tombstone_horizon) is expected
        if expected:
            rm.conforms(spec, "get", "/v1/sync/pull", rm.RefServer(spec).error("resync_required"))

    def test_guest_and_deleted_account_on_sync(self, spec, server, call):
        g = new_guest(server, call)
        a = sign_in(server, call, g, rm.AppleIdentity.new("sub-sync", server.now)).body
        # гость → 403 account_required; удалённый аккаунт → 401 account_deleted (общая проверка токена)
        for path, method in (("/v1/sync/push", "post"), ("/v1/sync/pull", "get")):
            r = server._auth(g["accessToken"], ("account",))
            rm.conforms(spec, method, path, r)
            assert r.code == "account_required"
        call("delete", "/v1/account", server.delete_account, a["accessToken"])
        r = server._auth(a["accessToken"], ("account",))
        rm.conforms(spec, "post", "/v1/sync/push", r)
        assert r.code == "account_deleted"
