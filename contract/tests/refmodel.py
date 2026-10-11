"""Эталонная модель поведения по контракту (только для контрактных тестов).

Не production-код: состояние в памяти, время — счётчик секунд, Apple и AI — заглушки. Модель
воплощает правила плана (§2, §3, §6) и `docs/api-contract.md`, а `conforms()` проверяет каждый её
ответ против `contract/openapi.yaml`: статус объявлен, тело проходит схему, код ошибки — из
`x-error-codes` этого ответа, обязательные заголовки есть. На этапах E2–E7 те же сценарии
запускаются против приложения через тот же интерфейс.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import contract_spec

DAY = 86_400
GRACE_S = 60
GUEST_REPLAY_S = 24 * 3600
ACCESS_TTL_S = 900
REFRESH_TTL_S = 365 * DAY
DELIVERY_TTL_S = 7 * DAY
UNCONFIRMED_MAX = 3
FREE_LIMIT = 3
MAX_FILE_BYTES = 2_097_152
DELETION_RETRY_AFTER_S = 60
DELETION_BATCH = 1000
DELETION_LEASE_S = 120
DELETION_BACKOFF_MAX_S = 3600
# Порядок шагов удаления (план §2.5): identities — последней пачкой вместе с переходом в done.
DELETION_STEPS = (
    "sync_records",
    "sync_mutations",
    "sync_state",
    "import_ledger",
    "apple_tokens",
    "devices",
    "users",
    "identities",
)
BUNDLE_ID = "ru.moneyplanner.app"


@dataclass
class Resp:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def code(self) -> str | None:
        return self.body["error"]["code"] if isinstance(self.body, dict) and "error" in self.body else None


def uuid7(ts: float) -> str:
    """UUIDv7: 48 бит миллисекунд Unix-времени + случайные биты."""
    ms = int(ts * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms << 80) | (0x7 << 76) | ((rand >> 68) & 0xFFF) << 64 | (0b10 << 62) | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def uuid7_time(value: str) -> float:
    return (uuid.UUID(value).int >> 80) / 1000


@dataclass
class AppleIdentity:
    """Учётные данные одной авторизации Apple на устройстве."""

    sub: str
    nonce: str
    code: str
    aud: str = BUNDLE_ID
    exp: float = 0.0

    @staticmethod
    def new(sub: str, now: float) -> AppleIdentity:
        return AppleIdentity(sub, secrets.token_hex(16), secrets.token_hex(8), exp=now + 600)

    def request(self) -> dict:
        token = {"sub": self.sub, "aud": self.aud, "exp": self.exp, "nonceHash": _sha(self.nonce)}
        return {"identityToken": token, "authorizationCode": self.code, "nonce": self.nonce}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class RefServer:
    def __init__(self, spec: dict):
        self.spec = spec
        self.catalog = spec["x-error-catalog"]
        self.now = 1_760_000_000.0
        # auth
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.families: dict[str, dict] = {}
        self.guest_keys: dict[str, dict] = {}
        self.devices: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        self.identities: dict[str, str] = {}
        self.used_nonces: set[str] = set()
        self.used_codes: set[str] = set()
        self.apple_refresh_tokens: dict[str, str] = {}
        self.deletions: dict[str, dict] = {}
        self.deleted_accounts: set[str] = set()
        self.sync_rows: dict[str, int] = {}  # число строк sync_records аккаунта
        self.apple_revoke = lambda token: 200  # ответ Apple /auth/revoke
        # imports
        self.deliveries: dict[tuple[str, str], dict] = {}
        self.ledger: dict[str, dict] = {}
        self.ai_calls = 0
        self.ai_answer = lambda key, attempt: None  # (categoryId, conf) | None
        self.ai_enabled = True
        self.parse_in_progress: set[tuple[str, str]] = set()
        self.server_key = secrets.token_bytes(32)

    # --- общие ------------------------------------------------------------------------------
    def tick(self, seconds: float) -> None:
        self.now += seconds

    def error(self, code: str, details: list | None = None, retry_after: int | None = None) -> Resp:
        e = self.catalog[code]
        rid = "req_" + uuid.uuid4().hex
        body = {
            "error": {
                "code": code,
                "message": code.replace("_", " ").capitalize(),
                "status": e["status"],
                "retryable": e["retryable"],
                "requestId": rid,
                "details": details or [],
            }
        }
        headers = {"X-Request-Id": rid}
        if e.get("retryAfter"):
            headers["Retry-After"] = str(retry_after if retry_after is not None else 1)
        return Resp(e["status"], body, headers)

    def ok(self, status: int, body: Any = None, **headers: str) -> Resp:
        return Resp(status, body, {"X-Request-Id": "req_" + uuid.uuid4().hex, **headers})

    def _issue(self, kind: str, did: str, account_id: str | None, family: str | None = None) -> dict:
        family = family or str(uuid.uuid4())
        self.families.setdefault(family, {"revoked": None, "kind": kind, "did": did, "account": account_id})
        access, rt = "at_" + secrets.token_hex(16), "rt_" + secrets.token_hex(16)
        self.access[access] = {
            "kind": kind,
            "did": did,
            "account": account_id,
            "family": family,
            "exp": self.now + ACCESS_TTL_S,
        }
        self.refresh[rt] = {"family": family, "exp": self.now + REFRESH_TTL_S, "rotated_to": None, "at": None}
        session = {
            "subjectKind": kind,
            "deviceId": did,
            "accessToken": access,
            "accessTokenExpiresAt": _ts(self.now + ACCESS_TTL_S),
            "refreshToken": rt,
            "refreshTokenExpiresAt": _ts(self.now + REFRESH_TTL_S),
        }
        if account_id:
            session["accountId"] = account_id
        return session

    def _auth(self, token: str | None, allowed: tuple[str, ...]) -> dict | Resp:
        a = self.access.get(token or "")
        if a is None:
            return self.error("access_token_invalid")
        if a["kind"] == "account":
            acc = self.accounts.get(a["account"])
            if acc is None or acc["state"] != "active" or a["account"] in self.deleted_accounts:
                return self.error("account_deleted")
            if self.families[a["family"]]["revoked"] == "session_revoked":
                return self.error("session_revoked")
        if a["exp"] <= self.now:
            return self.error("token_expired")
        if a["kind"] not in allowed:
            return self.error("account_required")
        self.devices[a["did"]]["last_seen"] = self.now
        return a

    # --- auth -------------------------------------------------------------------------------
    def guest(self, key: str | None, body: dict | None = None) -> Resp:
        try:
            uuid.UUID(key or "")
        except ValueError:
            return self.error("invalid_request")
        prev = self.guest_keys.get(key)
        if prev:
            row = self.refresh.get(prev["session"]["refreshToken"])
            fresh = self.now - prev["at"] <= GUEST_REPLAY_S and row and row["rotated_to"] is None
            if fresh and not self.families[row["family"]]["revoked"]:
                session = dict(prev["session"])
                access = "at_" + secrets.token_hex(16)
                self.access[access] = {**self.access[session["accessToken"]], "exp": self.now + ACCESS_TTL_S}
                session["accessToken"] = access
                return self.ok(201, session, **{"Idempotent-Replayed": "true"})
            return self.error("idempotency_key_reused")
        did = str(uuid.uuid4())
        self.devices[did] = {"last_seen": self.now, "account": None, "used": 0}
        session = self._issue("guest", did, None)
        self.guest_keys[key] = {"session": session, "at": self.now}
        return self.ok(201, session)

    def refresh_session(self, rt: str) -> Resp:
        row = self.refresh.get(rt)
        if row is None or row["exp"] <= self.now:
            return self.error("refresh_token_invalid")
        fam = self.families[row["family"]]
        if fam["account"] and self.accounts.get(fam["account"], {}).get("state") != "active":
            return self.error("account_deleted")
        if fam["revoked"] in ("session_revoked", "account_deleted"):
            return self.error(fam["revoked"])
        if fam["revoked"]:
            return self.error("refresh_token_invalid")
        if row["rotated_to"] is not None:
            if self.now - row["at"] <= GRACE_S:
                return self.ok(200, row["rotated_to"])
            fam["revoked"] = "reuse_detected"
            return self.error("refresh_token_invalid")
        session = self._issue(fam["kind"], fam["did"], fam["account"], family=row["family"])
        row["rotated_to"], row["at"] = session, self.now
        return self.ok(200, session)

    def apple(self, guest_access: str, req: dict) -> Resp:
        a = self._auth(guest_access, ("guest",))
        if isinstance(a, Resp):
            return a
        tok, nonce, code = req["identityToken"], req["nonce"], req["authorizationCode"]
        if tok["aud"] != BUNDLE_ID or tok["exp"] <= self.now or tok["nonceHash"] != _sha(nonce):
            return self.error("apple_token_invalid")
        if _sha(nonce) in self.used_nonces or code in self.used_codes:
            return self.error("apple_token_invalid")
        account_id = self.identities.get(tok["sub"])
        # Удаление в процессе: nonce и код не расходуются, удаление не отменяется.
        if account_id and self._deletion_pending(account_id):
            return self.error("account_deletion_in_progress", retry_after=DELETION_RETRY_AFTER_S)
        self.used_nonces.add(_sha(nonce))
        self.used_codes.add(code)  # обмен кода у Apple: одноразовый
        if account_id is None:
            account_id = str(uuid.uuid4())
            self.accounts[account_id] = {"state": "active", "records": 0, "last_version": 0, "used": 0}
            self.identities[tok["sub"]] = account_id
            self.sync_rows[account_id] = 0
        self.apple_refresh_tokens[account_id] = "apple_rt_for_" + code
        self.devices[a["did"]]["account"] = account_id
        self._attribute_guest_imports(a["did"], account_id)
        session = self._issue("account", a["did"], account_id)
        acc = self.accounts[account_id]
        session["sync"] = {"lastVersion": acc["last_version"], "hasData": acc["records"] > 0}
        return self.ok(200, session)

    def logout(self, rt: str) -> Resp:
        row = self.refresh.get(rt)
        if row:
            self.families[row["family"]]["revoked"] = self.families[row["family"]]["revoked"] or "logout"
        return self.ok(204)

    def consent_revoked(self, sub: str) -> None:
        account_id = self.identities[sub]
        for fam in self.families.values():
            if fam["account"] == account_id:
                fam["revoked"] = "session_revoked"

    # --- профиль и удаление -----------------------------------------------------------------
    def me(self, token: str) -> Resp:
        a = self._auth(token, ("guest", "account"))
        if isinstance(a, Resp):
            return a
        if a["kind"] == "guest":
            used = self.devices[a["did"]]["used"]
            body = {"subjectKind": "guest", "deviceId": a["did"]}
        else:
            acc = self.accounts[a["account"]]
            used = acc["used"]
            body = {
                "subjectKind": "account",
                "deviceId": a["did"],
                "accountId": a["account"],
                "sync": {"lastVersion": acc["last_version"]},
            }
        body["features"] = {"imports": self._usage(used)}
        return self.ok(200, body)

    def _deletion_pending(self, account_id: str) -> bool:
        return any(j["account"] == account_id and j["state"] != "done" for j in self.deletions.values())

    def delete_account(self, token: str, authorization_code: str | None = None) -> Resp:
        a = self.access.get(token or "")
        if a and a["kind"] == "account" and a["exp"] > self.now:
            job = next((j for j in self.deletions.values() if j["account"] == a["account"]), None)
            if job:  # повтор DELETE — тот же deletionId
                return self.ok(202, {"deletionId": job["id"], "state": job["state"]})
        a = self._auth(token, ("account",))
        if isinstance(a, Resp):
            return a
        # Синхронная часть: задание, users.state = deleting, отзыв сессий, deleted_accounts.
        job_id = str(uuid.uuid4())
        apple_token = self.apple_refresh_tokens.get(a["account"]) or (
            "apple_rt_for_" + authorization_code if authorization_code else None
        )
        self.deletions[job_id] = {
            "id": job_id,
            "account": a["account"],
            "did": a["did"],
            "state": "revoking",
            "revocation": "pending" if apple_token else "skipped_no_token",
            "apple_token": apple_token,
            "step": 0,
            "cursor": 0,
            "attempts": 0,
            "lease_until": 0.0,
            "next_run": 0.0,
            "updated": self.now,
            "batches": 0,
        }
        self.accounts[a["account"]]["state"] = "deleting"
        self.deleted_accounts.add(a["account"])
        for fam in self.families.values():
            if fam["account"] == a["account"]:
                fam["revoked"] = "account_deleted"
        return self.ok(202, {"deletionId": job_id, "state": "revoking"})

    def run_deletions(self, max_batches: int | None = None) -> None:
        """Фоновое продолжение (/internal/deletions/run): аренда, пачки, курсор после каждой пачки.

        max_batches — сколько пачек успевает экземпляр до «падения»: аренда при этом не снимается,
        следующий запуск продолжит с сохранённого курсора после истечения аренды.
        """
        budget = max_batches
        for job in self.deletions.values():
            if job["state"] == "done" or job["lease_until"] > self.now or job["next_run"] > self.now:
                continue
            job["lease_until"] = self.now + DELETION_LEASE_S
            acc_id = job["account"]
            # Шаг отзыва Apple независим от удаления данных; задание завершается после обоих.
            if job["revocation"] == "pending":
                status = self.apple_revoke(job["apple_token"])
                if status == 200:
                    job["revocation"] = "done"
                else:  # сеть, 5xx, 400 invalid_client: повтор с backoff (и алерт)
                    job["attempts"] += 1
                    job["next_run"] = self.now + min(DELETION_BACKOFF_MAX_S, 60 * 2 ** job["attempts"])
                    job["state"] = "failed_retrying"
            while job["step"] < len(DELETION_STEPS):
                if budget is not None and budget <= 0:
                    return  # экземпляр «упал»: аренда остаётся до истечения
                step = DELETION_STEPS[job["step"]]
                if step == "identities" and job["revocation"] == "pending":
                    break  # последний шаг — только после отзыва
                done_step = self._delete_batch(step, acc_id, job)
                job["batches"] += 1
                if budget is not None:
                    budget -= 1
                if done_step:
                    job["step"], job["cursor"] = job["step"] + 1, 0
                if job["state"] != "failed_retrying":
                    job["state"] = "deleting"
                job["updated"] = self.now
            if job["step"] == len(DELETION_STEPS):
                job["state"] = "done"
            job["lease_until"] = 0.0
            job["updated"] = self.now

    def _delete_batch(self, step: str, acc_id: str, job: dict) -> bool:
        """Одна транзакция до DELETION_BATCH строк. True — шаг завершён."""
        if step == "sync_records":
            left = self.sync_rows.get(acc_id, 0) - DELETION_BATCH
            self.sync_rows[acc_id] = max(0, left)
            job["cursor"] += DELETION_BATCH
            return left <= 0
        if step == "import_ledger":
            # Записи с account_id = A и счётчик аккаунта; счётчик устройства остаётся.
            self.ledger = {k: r for k, r in self.ledger.items() if r["account"] != acc_id}
        elif step == "apple_tokens":
            self.apple_refresh_tokens.pop(acc_id, None)
        elif step == "devices":
            for dev in self.devices.values():
                if dev["account"] == acc_id:
                    dev["account"] = None
        elif step == "users":
            self.accounts.pop(acc_id, None)
        elif step == "identities":
            self.identities = {s: a for s, a in self.identities.items() if a != acc_id}
        return True

    def deletion_status(self, token: str, deletion_id: str) -> Resp:
        a = self._auth(token, ("guest",))
        if isinstance(a, Resp):
            return a
        job = self.deletions.get(deletion_id)
        if job is None or job["did"] != a["did"]:
            return self.error("deletion_not_found")
        return self.ok(
            200,
            {
                "deletionId": job["id"],
                "state": job["state"],
                "appleTokenRevocation": job["revocation"],
                "updatedAt": _ts(job["updated"]),
            },
        )

    # --- импорт -----------------------------------------------------------------------------
    def _usage(self, used: int) -> dict:
        return {
            "used": used,
            "limit": FREE_LIMIT,
            "state": "free" if used < FREE_LIMIT else "quota_exhausted",
            "enforced": False,
        }

    def _attribute_guest_imports(self, did: str, account_id: str) -> None:
        for rec in self.ledger.values():
            if rec["device"] == did and rec["account"] is None and rec["attributed"] is None:
                rec["attributed"] = account_id
                self.accounts[account_id]["used"] += 1

    def parse(
        self,
        token: str,
        key: str | None,
        file: bytes,
        categories: list[dict],
        hints: list[dict] | None = None,
    ) -> Resp:
        a = self._auth(token, ("guest", "account"))
        if isinstance(a, Resp):
            return a
        try:
            uuid.UUID(key or "")
        except ValueError:
            return self.error("invalid_request")
        if len(file) > MAX_FILE_BYTES:
            return self.error("file_too_large")
        did = a["did"]
        mac = hmac.new(self.server_key, file, hashlib.sha256).hexdigest()
        if (did, key) in self.parse_in_progress:
            return self.error("idempotency_in_progress", retry_after=2)
        delivery = self.deliveries.get((did, key))
        if delivery and self.now - delivery["created"] > DELIVERY_TTL_S:
            delivery = None
        if delivery and delivery["mac"] != mac:
            return self.error("idempotency_key_reused")
        ops = _parse_statement(file)
        if isinstance(ops, str):
            return self.error(ops)  # ошибка разбора: доставка не создаётся, попытка не расходуется
        if delivery is None:
            recent = [
                d
                for d in self.deliveries.values()
                if d["device"] == did and d["confirmed"] is None and self.now - d["created"] < DAY
            ]
            if len(recent) >= UNCONFIRMED_MAX:
                oldest = min(d["created"] for d in recent)
                return self.error("unconfirmed_imports_limit", retry_after=int(oldest + DAY - self.now) + 1)
            delivery = {
                "import_id": uuid7(self.now),
                "device": did,
                "mac": mac,
                "account": a["account"],
                "created": self.now,
                "confirmed": None,
                "attempt": 0,
            }
            self.deliveries[(did, key)] = delivery
        delivery["attempt"] += 1
        operations = self._categorize(ops, categories, hints or [], delivery["attempt"])
        used = self.accounts[a["account"]]["used"] if a["account"] else self.devices[did]["used"]
        body = {
            "importId": delivery["import_id"],
            "confirmed": delivery["confirmed"] is not None,
            "parserVersion": "alfa-xlsx/1",
            "bank": {"code": "alfa", "name": "Альфа-Банк"},
            "period": {"from": min(o["date"] for o in ops), "to": max(o["date"] for o in ops)},
            "operations": operations,
            "skippedIncomeCount": 0,
            "warnings": [],
            "imports": self._usage(used),
        }
        return self.ok(200, body)

    def _categorize(self, ops: list[dict], categories: list[dict], hints: list[dict], attempt: int) -> list:
        ids = {c["id"] for c in categories}
        by_hint = {h["merchantKey"]: h["categoryId"] for h in hints if h["categoryId"] in ids}
        out = []
        for op in ops:
            key = op["comment"].lower()
            cid, status = by_hint.get(key), "assigned"
            if cid is None and self.ai_enabled:
                self.ai_calls += 1
                guess = self.ai_answer(key, attempt)
                if guess and guess[0] in ids and guess[1] in ("high", "medium"):
                    cid, status = guess[0], "assigned" if guess[1] == "high" else "suggested"
            if cid is None:
                status = "unassigned"
            out.append({**op, "categoryId": cid, "categoryStatus": status})
        return out

    def displayed(self, token: str, import_id: str) -> Resp:
        a = self._auth(token, ("guest", "account"))
        if isinstance(a, Resp):
            return a
        delivery = next(
            (d for d in self.deliveries.values() if d["import_id"] == import_id and d["device"] == a["did"]),
            None,
        )
        if delivery and self.now - delivery["created"] > DELIVERY_TTL_S:
            del self.deliveries[next(k for k, d in self.deliveries.items() if d is delivery)]  # TTL
            delivery = None
        if delivery is None:
            # Строки нет: по времени в UUIDv7 отличаем истёкшую доставку от несуществующей.
            if self.now - uuid7_time(import_id) > DELIVERY_TTL_S:
                return self.error("delivery_expired")
            return self.error("import_not_found")
        if delivery["confirmed"] is None:
            delivery["confirmed"] = self.now
            self.ledger[import_id] = {
                "device": delivery["device"],
                "account": delivery["account"],
                "attributed": None,
            }
            self.devices[delivery["device"]]["used"] += 1
            if delivery["account"]:
                self.accounts[delivery["account"]]["used"] += 1
        used = self.accounts[a["account"]]["used"] if a["account"] else self.devices[a["did"]]["used"]
        return self.ok(200, {"imports": self._usage(used)})


def _ts(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _parse_statement(file: bytes) -> list[dict] | str:
    """Заглушка парсера: строки «YYYY-MM-DD;сумма;название». Детерминирована по содержимому."""
    if file.startswith(b"BAD"):
        return "unknown_bank"
    ops = []
    for line in file.decode().splitlines():
        d, amount, comment = line.split(";")
        ops.append({"date": d, "amount": amount, "comment": comment})
    return ops or "empty_statement"


# --- клиент: черновик импорта (план §6.5) ------------------------------------------------------
class RefClientDrafts:
    def __init__(self):
        self.drafts: dict[str, dict] = {}
        self.confirm_queue: list[str] = []

    def on_parse_response(self, body: dict) -> dict:
        iid = body["importId"]
        if iid in self.drafts:  # черновик не перезаписывается
            draft = self.drafts[iid]
            draft["confirmed"] = body["confirmed"]
            draft["imports"] = body["imports"]
            return draft
        draft = {
            "operations": [dict(op) for op in body["operations"]],
            "parserVersion": body["parserVersion"],
            "confirmed": body["confirmed"],
            "imports": body["imports"],
        }
        self.drafts[iid] = draft
        return draft

    def on_displayed(self, import_id: str) -> None:
        if not self.drafts[import_id]["confirmed"] and import_id not in self.confirm_queue:
            self.confirm_queue.append(import_id)


# --- соответствие ответа контракту --------------------------------------------------------------
def conforms(spec: dict, method: str, path: str, resp: Resp) -> None:
    op = contract_spec.operations(spec)[(method, path)]
    raw = op["responses"].get(str(resp.status))
    assert raw is not None, f"{method.upper()} {path}: статус {resp.status} не объявлен"
    declared = contract_spec.resolve(spec, raw)
    for name, h in declared.get("headers", {}).items():
        if contract_spec.resolve(spec, h).get("required"):
            assert name in resp.headers, f"{method.upper()} {path} {resp.status}: нет заголовка {name}"
    codes = declared.get("x-error-codes")
    if codes is not None:
        assert resp.code in codes, f"{method.upper()} {path} {resp.status}: код {resp.code} не из {codes}"
    media = declared.get("content", {}).get("application/json")
    if media is None:
        assert resp.body is None
        return
    schema = media["schema"]
    pointer = schema["$ref"][1:]
    errors = [e.message for e in contract_spec.schema_validator(spec, pointer).iter_errors(resp.body)]
    assert not errors, f"{method.upper()} {path} {resp.status}: {errors}"


# --- клиент: результат входа Apple (план §2.3, §2.9) --------------------------------------------
def client_after_sign_in(saved_account_id: str | None, local_has_data: bool, body: dict) -> str:
    """Что делает клиент после 200 /auth/apple. Outbox до этого момента не отправлялся."""
    if saved_account_id is None:
        if local_has_data and body["sync"]["hasData"]:
            return "merge_or_replace_dialog"
        return "upload_local" if local_has_data else "full_download"
    if body["accountId"] == saved_account_id:
        return "continue_sync"  # reauth_required снят, outbox уходит с прежними mutationId
    return "manual_logout_warning"


# --- sync: граница валидации push (docs/api-contract.md §8) и условие 410 pull ------------------
NS_DAY_MARK = uuid.UUID("97a5414e-31da-4b51-85c7-7ea3eab0c81d")
MAX_PUSH_BYTES = 1_048_576
KNOWN_SCHEMA_VERSIONS = {1}


def classify_push(
    spec: dict,
    body: dict,
    size: int,
    registry: dict,
    today: str,
    existing_kinds: dict[str, str] | None = None,
) -> Resp | list[dict | None]:
    """Ошибка всего запроса (Resp) или вердикт по каждой мутации: None — к движку, dict — rejected/invalid."""
    import datetime as dt

    server = RefServer(spec)
    mutations = body.get("mutations")
    if size > MAX_PUSH_BYTES or (isinstance(mutations, list) and len(mutations) > 500):
        return server.error("payload_too_large")
    device_v = contract_spec.schema_validator(spec, "/components/schemas/PushRequest/properties/deviceId")
    if not isinstance(mutations, list) or not mutations or list(device_v.iter_errors(body.get("deviceId"))):
        return server.error("validation_failed", [{"code": "invalid", "field": "mutations"}])
    base_v = contract_spec.schema_validator(spec, "/components/schemas/MutationBase")
    details = []
    for i, m in enumerate(mutations):
        for err in base_v.iter_errors(m):
            field = err.absolute_path[0] if err.absolute_path else None
            code = "unknown_entity_type" if field == "entityType" else "invalid"
            details.append({"code": code, "field": field or "mutation", "index": i})
    if details:
        return server.error("validation_failed", details)
    if any(m["schemaVersion"] not in KNOWN_SCHEMA_VERSIONS for m in mutations):
        return server.error("upgrade_required")

    system_ids = {c["id"] for c in registry["categories"]}
    full_v = contract_spec.schema_validator(spec, "/components/schemas/Mutation")
    max_date = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()
    verdicts: list[dict | None] = []
    for m in mutations:
        d: list[dict] = []
        payload = m.get("payload") or {}
        if m["entityType"] in ("category", "day_mark") and m["op"] == "delete":
            d.append({"code": "op_not_allowed", "field": "op"})
        elif list(full_v.iter_errors(m)):
            if (
                m["entityType"] == "expense"
                and isinstance(payload.get("comment"), str)
                and len(payload["comment"]) > 200
            ):
                d.append({"code": "comment_too_long", "field": "comment"})
            else:
                d.append({"code": "payload_invalid", "field": "payload"})
        elif m["entityType"] == "category":
            kind, eid = payload["kind"], m["entityId"]
            if kind == "system" and eid not in system_ids:
                d.append({"code": "system_category_unknown", "field": "entityId"})
            elif kind == "user" and eid in system_ids:
                d.append({"code": "category_kind_mismatch", "field": "kind"})
            elif (existing_kinds or {}).get(eid, kind) != kind:
                d.append({"code": "category_kind_change", "field": "kind"})
            elif eid == registry["fallbackCategoryId"] and payload["isArchived"]:
                d.append({"code": "fallback_category_not_archivable", "field": "isArchived"})
        elif m["entityType"] == "day_mark":
            if m["entityId"] != str(uuid.uuid5(NS_DAY_MARK, payload["date"])):
                d.append({"code": "day_mark_id_mismatch", "field": "entityId"})
            elif payload["date"] > max_date:
                d.append({"code": "date_in_future", "field": "date"})
        verdicts.append(
            {
                "mutationId": m["mutationId"],
                "status": "rejected",
                "reason": "invalid",
                "conflict": False,
                "replayed": False,
                "details": d,
            }
            if d
            else None
        )
    return verdicts


def needs_resync(cursor: int, client_horizon: int, tombstone_horizon: int) -> bool:
    """410 resync_required (architecture §8.2)."""
    return cursor > 0 and cursor < tombstone_horizon and client_horizon < tombstone_horizon
