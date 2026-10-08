"""Контракт sync API и внутренние структуры движка.

Внешний JSON — camelCase (как в iOS), внутри — dataclass-ы без валидации, чтобы симуляция
на тысячах историй работала быстро.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from pydantic.alias_generators import to_camel

EntityType = Literal["expense", "category"]
Op = Literal["upsert", "delete"]

MAX_MUTATIONS_PER_PUSH = 500
MAX_PULL_LIMIT = 500
MAX_PUSH_BODY_BYTES = 1_048_576


class _Api(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


# --- payload сущностей (сервер проверяет форму, но не бизнес-логику) ---------------------------


class ExpensePayload(_Api):
    amount: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
    category_id: uuid.UUID
    date: dt.date
    comment: Annotated[str, StringConstraints(max_length=500)] | None = None


class CategoryPayload(_Api):
    name: Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
    emoji: Annotated[str, StringConstraints(max_length=16)] | None = None


PAYLOAD_MODELS: dict[str, type[_Api]] = {"expense": ExpensePayload, "category": CategoryPayload}


class MutationIn(_Api):
    mutation_id: uuid.UUID
    entity_type: EntityType
    entity_id: uuid.UUID
    op: Op
    # Вариант B: серверная версия записи, на которой основано изменение (0 — новая запись).
    base_version: Annotated[int, Field(ge=0)] = 0
    # Вариант A: HLC клиента. Формат "<ms:15>.<counter:6>".
    hlc: Annotated[str, StringConstraints(pattern=r"^\d{15}\.\d{6}$")] | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None = None
    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
    payload: dict[str, Any] | None = None

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _aware(cls, v: dt.datetime | None) -> dt.datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return v

    @model_validator(mode="after")
    def _shape(self) -> MutationIn:
        if self.op == "delete":
            if self.deleted_at is None or self.payload is not None:
                raise ValueError("delete requires deletedAt and null payload")
        else:
            if self.deleted_at is not None or self.payload is None:
                raise ValueError("upsert requires payload and null deletedAt")
            # Нормализуем payload через модель: в БД попадает только известная форма.
            model = PAYLOAD_MODELS[self.entity_type].model_validate(self.payload)
            self.payload = model.model_dump(mode="json", by_alias=True, exclude_none=True)
        return self


class PushRequest(_Api):
    device_id: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
    mutations: Annotated[list[MutationIn], Field(min_length=1, max_length=MAX_MUTATIONS_PER_PUSH)]


class RecordOut(_Api):
    entity_type: EntityType
    entity_id: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None
    hlc: str | None
    device_id: str
    schema_version: int
    payload: dict[str, Any] | None


Status = Literal["applied", "rejected", "noop"]
Reason = Literal["conflict", "deleted", "stale", "mutation_id_reused"]


class MutationResultOut(_Api):
    mutation_id: str
    status: Status
    reason: Reason | None = None
    conflict: bool = False
    version: int | None = None
    replayed: bool = False
    # Актуальная серверная запись, если она отличается от того, что применил клиент.
    record: RecordOut | None = None


class PushResponse(_Api):
    results: list[MutationResultOut]


class PullResponse(_Api):
    records: list[RecordOut]
    next_cursor: int
    has_more: bool


# --- внутреннее представление -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mutation:
    mutation_id: str
    entity_type: str
    entity_id: str
    op: str
    base_version: int
    hlc: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None
    schema_version: int
    payload: dict[str, Any] | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity_type, self.entity_id)

    def request_hash(self, device_id: str) -> str:
        body = {
            "d": device_id,
            "t": self.entity_type,
            "id": self.entity_id,
            "op": self.op,
            "b": self.base_version,
            "h": self.hlc,
            "c": _iso(self.created_at),
            "u": _iso(self.updated_at),
            "x": _iso(self.deleted_at),
            "s": self.schema_version,
            "p": self.payload,
        }
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_api(cls, m: MutationIn) -> Mutation:
        return cls(
            mutation_id=str(m.mutation_id),
            entity_type=m.entity_type,
            entity_id=str(m.entity_id),
            op=m.op,
            base_version=m.base_version,
            hlc=m.hlc,
            created_at=_utc(m.created_at),
            updated_at=_utc(m.updated_at),
            deleted_at=_utc(m.deleted_at) if m.deleted_at else None,
            schema_version=m.schema_version,
            payload=m.payload,
        )


@dataclass(frozen=True, slots=True)
class StoredRecord:
    entity_type: str
    entity_id: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None
    # Вариант A: HLC последнего применённого изменения (после обрезки сервером).
    hlc: str | None
    # Вариант B: монотонная «логическая» метка записи = max(updatedAt клиента, order_ts основы).
    order_ts: dt.datetime
    device_id: str
    schema_version: int
    payload: dict[str, Any] | None
    mutation_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity_type, self.entity_id)

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    def to_api(self) -> RecordOut:
        return RecordOut(
            entity_type=self.entity_type,  # type: ignore[arg-type]
            entity_id=self.entity_id,
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
            deleted_at=self.deleted_at,
            hlc=self.hlc,
            device_id=self.device_id,
            schema_version=self.schema_version,
            payload=self.payload,
        )


@dataclass(slots=True)
class MutationResult:
    mutation_id: str
    status: str
    reason: str | None = None
    conflict: bool = False
    version: int | None = None
    replayed: bool = False
    record: StoredRecord | None = None
    # Служебное: на какой версии основано применённое изменение (для перебазирования в пачке).
    base_version: int | None = None

    def stored_json(self) -> dict[str, Any]:
        """То, что кладём в sync_mutations: без записи — она на повторе берётся свежей."""
        return {
            "status": self.status,
            "reason": self.reason,
            "conflict": self.conflict,
            "version": self.version,
            "base": self.base_version,
        }

    @classmethod
    def from_stored(cls, mutation_id: str, data: dict[str, Any]) -> MutationResult:
        return cls(
            mutation_id=mutation_id,
            status=data["status"],
            reason=data.get("reason"),
            conflict=bool(data.get("conflict")),
            version=data.get("version"),
            base_version=data.get("base"),
            replayed=True,
        )

    def to_api(self) -> MutationResultOut:
        return MutationResultOut(
            mutation_id=self.mutation_id,
            status=self.status,  # type: ignore[arg-type]
            reason=self.reason,  # type: ignore[arg-type]
            conflict=self.conflict,
            version=self.version,
            replayed=self.replayed,
            record=self.record.to_api() if self.record else None,
        )


@dataclass(slots=True)
class PushPlan:
    """Результат чистого планирования пачки: что записать и что ответить."""

    results: list[MutationResult]
    upserts: dict[tuple[str, str], StoredRecord] = field(default_factory=dict)
    new_logs: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)  # (mid, hash, result)
    last_version: int = 0
    changed: bool = False


def _utc(v: dt.datetime) -> dt.datetime:
    return v.astimezone(dt.UTC)


def _iso(v: dt.datetime | None) -> str | None:
    return v.astimezone(dt.UTC).isoformat() if v else None
