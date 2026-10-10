"""Контракт sync API и внутренние структуры движка.

Внешний JSON — camelCase (как в iOS), внутри — dataclass-ы без валидации, чтобы симуляция
на тысячах историй работала быстро.
"""

# Модуль используется везде: API (pydantic-схемы), движок и хранилища (dataclass-ы), клиент (формат JSON).

from __future__ import annotations

# dt — даты; hashlib/json — отпечаток мутации; uuid — идентификаторы; Decimal — суммы.
import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal

# Pydantic: схемы и проверка входящих запросов; to_camel — имена полей JSON в camelCase.
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from pydantic.alias_generators import to_camel

# Синхронизируемые сущности: только расходы и категории. app_settings, черновик импорта и состояние
# UI остаются на устройстве и через sync не передаются (типа для них нет — сервер вернёт 422).
# Удаление — это tombstone, а не DELETE.
EntityType = Literal["expense", "category"]
Op = Literal["upsert", "delete"]

# Лимиты протокола: мутаций в одном push, записей на странице pull, байт в теле push.
MAX_MUTATIONS_PER_PUSH = 500
MAX_PULL_LIMIT = 500
MAX_PUSH_BODY_BYTES = 1_048_576


# Базовая схема API: camelCase в JSON, лишние поля запрещены (нельзя подсунуть, например, userId).
class _Api(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


# --- payload сущностей (сервер проверяет форму, но не бизнес-логику) ---------------------------


# Форма расхода: сумма > 0 с 2 знаками, категория, дата, комментарий до 500 символов.
# Единая сущность Expense: ручные и импортированные расходы не различаются. У импортированного
# расхода название операции из выписки записано в comment; после сохранения он ведёт себя как ручной.
class ExpensePayload(_Api):
    amount: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
    category_id: uuid.UUID
    date: dt.date
    comment: Annotated[str, StringConstraints(max_length=500)] | None = None


# Форма категории: название 1–64 символа, эмодзи необязателен.
# isArchived — «удалённая» пользователем категория: недоступна для новых расходов, но существующие
# расходы и аналитика продолжают на неё ссылаться. Синхронизируется как обычное поле.
class CategoryPayload(_Api):
    name: Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
    emoji: Annotated[str, StringConstraints(max_length=16)] | None = None
    is_archived: bool = False


# Какой схемой проверять payload для каждого типа сущности.
PAYLOAD_MODELS: dict[str, type[_Api]] = {"expense": ExpensePayload, "category": CategoryPayload}


# Одна мутация (изменение записи) в запросе push.
class MutationIn(_Api):
    # Уникальный id мутации — ключ идемпотентности: повтор с тем же id вернёт тот же результат.
    mutation_id: uuid.UUID
    # Тип и id записи; id генерирует клиент (UUID), поэтому повтор не может создать дубль.
    entity_type: EntityType
    entity_id: uuid.UUID
    op: Op
    # Серверная версия записи, на которой основано изменение (0 — новая запись).
    base_version: Annotated[int, Field(ge=0)] = 0
    # Время создания записи и время изменения (часы клиента; только для конкурентных конфликтов).
    created_at: dt.datetime
    updated_at: dt.datetime
    # Заполняется только для op=delete.
    deleted_at: dt.datetime | None = None
    # Версия формата payload (пока одна).
    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
    payload: dict[str, Any] | None = None

    # Все времена обязаны быть с часовым поясом — иначе непонятно, какое это время.
    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _aware(cls, v: dt.datetime | None) -> dt.datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return v

    # Согласованность полей: delete — с deletedAt и без payload; upsert — наоборот.
    @model_validator(mode="after")
    def _shape(self) -> MutationIn:
        if self.op == "delete":
            if self.deleted_at is None or self.payload is not None:
                raise ValueError("delete requires deletedAt and null payload")
            # Категория не удаляется, а архивируется (upsert с isArchived=true).
            if self.entity_type == "category":
                raise ValueError("categories are archived (isArchived=true), not deleted")
        else:
            if self.deleted_at is not None or self.payload is None:
                raise ValueError("upsert requires payload and null deletedAt")
            # Нормализуем payload через модель: в БД попадает только известная форма.
            model = PAYLOAD_MODELS[self.entity_type].model_validate(self.payload)
            self.payload = model.model_dump(mode="json", by_alias=True, exclude_none=True)
        return self


# Тело POST /sync/push. userId в теле нет — он берётся из токена.
class PushRequest(_Api):
    # Имя устройства: короткое, только безопасные символы.
    device_id: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
    mutations: Annotated[list[MutationIn], Field(min_length=1, max_length=MAX_MUTATIONS_PER_PUSH)]


# Запись в ответах pull и push — так её видит клиент.
class RecordOut(_Api):
    entity_type: EntityType
    entity_id: str
    # Серверная версия — она же курсор для pull.
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None
    device_id: str
    schema_version: int
    payload: dict[str, Any] | None


# Итог мутации: applied — применена; rejected — отклонена (с причиной); noop — ничего не изменилось.
Status = Literal["applied", "rejected", "noop"]
# Причины отказа: конфликт, запись удалена, повтор mutationId с другим телом.
Reason = Literal["conflict", "deleted", "mutation_id_reused"]


# Результат одной мутации в ответе push.
class MutationResultOut(_Api):
    mutation_id: str
    status: Status
    reason: Reason | None = None
    # conflict — сервер обнаружил конкурентную правку (даже если мутация в итоге применена).
    conflict: bool = False
    version: int | None = None
    # replayed — это повтор уже обработанной мутации, ответ взят из журнала.
    replayed: bool = False
    # Актуальная серверная запись, если она отличается от того, что применил клиент.
    record: RecordOut | None = None


# Ответ push: результаты в том же порядке, что мутации в запросе.
class PushResponse(_Api):
    results: list[MutationResultOut]


# Ответ pull: страница записей, курсор для следующего запроса, признак, есть ли ещё, и текущий
# горизонт очистки tombstones (клиент присылает его в следующем pull как `horizon`).
class PullResponse(_Api):
    records: list[RecordOut]
    next_cursor: int
    has_more: bool
    horizon: int


# --- внутреннее представление -----------------------------------------------------------------


# Мутация внутри движка — без валидации, быстро (используется и в симуляции).
@dataclass(frozen=True, slots=True)
class Mutation:
    mutation_id: str
    entity_type: str
    entity_id: str
    op: str
    base_version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None
    schema_version: int
    payload: dict[str, Any] | None

    # Ключ записи (тип, id).
    @property
    def key(self) -> tuple[str, str]:
        return (self.entity_type, self.entity_id)

    # Отпечаток содержимого мутации: повтор того же mutationId с другим телом — ошибка клиента.
    def request_hash(self, device_id: str) -> str:
        body = {
            "d": device_id,
            "t": self.entity_type,
            "id": self.entity_id,
            "op": self.op,
            "b": self.base_version,
            "c": _iso(self.created_at),
            "u": _iso(self.updated_at),
            "x": _iso(self.deleted_at),
            "s": self.schema_version,
            "p": self.payload,
        }
        # Канонический JSON: одинаковые данные — одинаковая строка.
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    # Из проверенной схемы API во внутреннее представление (все времена — в UTC).
    @classmethod
    def from_api(cls, m: MutationIn) -> Mutation:
        return cls(
            mutation_id=str(m.mutation_id),
            entity_type=m.entity_type,
            entity_id=str(m.entity_id),
            op=m.op,
            base_version=m.base_version,
            created_at=_utc(m.created_at),
            updated_at=_utc(m.updated_at),
            deleted_at=_utc(m.deleted_at) if m.deleted_at else None,
            schema_version=m.schema_version,
            payload=m.payload,
        )


# Запись так, как она хранится на сервере (строка sync_records).
@dataclass(frozen=True, slots=True)
class StoredRecord:
    entity_type: str
    entity_id: str
    # Версия последнего изменения (монотонная внутри пользователя).
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None
    # Монотонная «логическая» метка записи = max(updatedAt клиента, order_ts основы).
    order_ts: dt.datetime
    # Устройство, сделавшее последнее изменение; mutation_id — каким изменением получено состояние.
    device_id: str
    schema_version: int
    payload: dict[str, Any] | None
    mutation_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity_type, self.entity_id)

    # Запись удалена (tombstone).
    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    # В формат ответа API.
    def to_api(self) -> RecordOut:
        return RecordOut(
            entity_type=self.entity_type,  # type: ignore[arg-type]
            entity_id=self.entity_id,
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
            deleted_at=self.deleted_at,
            device_id=self.device_id,
            schema_version=self.schema_version,
            payload=self.payload,
        )


# Результат обработки одной мутации внутри движка.
@dataclass(slots=True)
class MutationResult:
    mutation_id: str
    status: str
    reason: str | None = None
    conflict: bool = False
    version: int | None = None
    replayed: bool = False
    # Актуальная серверная запись — прикладывается к ответу, если клиенту нужно её применить.
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

    # Восстановить результат из журнала sync_mutations (при повторе).
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

    # В формат ответа API.
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


# Время в UTC.
def _utc(v: dt.datetime) -> dt.datetime:
    return v.astimezone(dt.UTC)


# Время в ISO-строке UTC (для отпечатка мутации).
def _iso(v: dt.datetime | None) -> str | None:
    return v.astimezone(dt.UTC).isoformat() if v else None
