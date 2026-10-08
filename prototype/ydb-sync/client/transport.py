"""Транспорты тестового клиента.

- HttpTransport — настоящий HTTP (in-process через ASGI или реальный URL контейнера).
- DirectTransport — тот же JSON-контракт без HTTP, для быстрой симуляции тысяч историй.
- FaultyTransport — обёртка с внедрением сбоев: offline, потеря ответа после обработки
  сервером, обрыв pull после N страниц.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from syncproto.engine import ResyncRequired, SyncEngine
from syncproto.models import Mutation, PullResponse, PushRequest, PushResponse


class TransportError(Exception):
    """Сеть недоступна или ответ потерян. Клиент не знает, обработал ли сервер запрос."""


class ResyncRequiredError(Exception):
    pass


class Transport(Protocol):
    async def push(self, body: dict[str, Any]) -> dict[str, Any]: ...
    async def pull(self, cursor: int, limit: int) -> dict[str, Any]: ...


@dataclass
class TrafficStats:
    requests: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    last_headers: dict[str, str] = field(default_factory=dict)


class HttpTransport:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.stats = TrafficStats()

    async def _send(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        headers = {**self.headers, **kw.pop("headers", {})}
        try:
            r = await self.client.request(method, url, headers=headers, **kw)
        except httpx.TransportError as e:
            raise TransportError(str(e)) from e
        self.stats.requests += 1
        self.stats.bytes_sent += len(r.request.content or b"")
        self.stats.bytes_received += len(r.content)
        self.stats.last_headers = dict(r.headers)
        if r.status_code == 410:
            raise ResyncRequiredError()
        if r.status_code >= 500:
            raise TransportError(f"server error {r.status_code}")
        r.raise_for_status()
        return r.json()

    async def push(self, body: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        return await self._send(
            "POST", "/sync/push", content=content, headers={"Content-Type": "application/json"}
        )

    async def pull(self, cursor: int, limit: int) -> dict[str, Any]:
        return await self._send("GET", "/sync/pull", params={"cursor": cursor, "limit": limit})


class DirectTransport:
    def __init__(self, engine: SyncEngine, user_id: str) -> None:
        self.engine = engine
        self.user_id = user_id

    async def push(self, body: dict[str, Any]) -> dict[str, Any]:
        req = PushRequest.model_validate(body)
        results, _ = await self.engine.push(
            self.user_id, req.device_id, [Mutation.from_api(m) for m in req.mutations]
        )
        return PushResponse(results=[r.to_api() for r in results]).model_dump(mode="json", by_alias=True)

    async def pull(self, cursor: int, limit: int) -> dict[str, Any]:
        try:
            page, _ = await self.engine.pull(self.user_id, cursor, limit)
        except ResyncRequired as e:
            raise ResyncRequiredError() from e
        return PullResponse(
            records=[r.to_api() for r in page.records], next_cursor=page.next_cursor, has_more=page.has_more
        ).model_dump(mode="json", by_alias=True)


class FaultyTransport:
    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.offline = False
        self.drop_push_responses = 0  # сколько ответов push «потерять» ПОСЛЕ обработки сервером
        self.fail_pull_after_pages: int | None = None
        self._pages = 0

    def fail_pull_after(self, pages: int) -> None:
        self.fail_pull_after_pages = pages
        self._pages = 0

    async def push(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.offline:
            raise TransportError("offline")
        resp = await self.inner.push(body)
        if self.drop_push_responses > 0:
            self.drop_push_responses -= 1
            raise TransportError("response lost")
        return resp

    async def pull(self, cursor: int, limit: int) -> dict[str, Any]:
        if self.offline:
            raise TransportError("offline")
        if self.fail_pull_after_pages is not None and self._pages >= self.fail_pull_after_pages:
            self.fail_pull_after_pages = None
            self._pages = 0
            raise TransportError("connection reset during pull")
        self._pages += 1
        return await self.inner.pull(cursor, limit)
