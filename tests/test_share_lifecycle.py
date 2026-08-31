from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from threading import Event

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope
from typing_extensions import override

from groken.share_client import RelayManager, ShareLink
from groken.share_server import create_share_app
from groken.share_store import ShareRecord


class FiniteFeed:
    def __init__(self) -> None:
        event: dict[str, object] = {
            "event": "agent-update",
            "data": {"agentId": "bot-id"},
        }
        self._events: Iterator[dict[str, object]] = iter([event])

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del timeout_s, hold
        return next(self._events)

    def resume(self) -> None:
        return None


class LifecycleManager:
    def __init__(self) -> None:
        self.close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        raise AssertionError((method, args))

    def send_prompt(self, agent_id: str, text: str) -> dict[str, object]:
        raise AssertionError((agent_id, text))

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        raise AssertionError((agent_id, text, timeout_s))

    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        del agent_id, text, timeout_s, idle_s
        assert self.close_calls == 0
        if on_chunk is not None:
            on_chunk("part")
        return "reply"

    def transcript_tail(self, agent_id: str) -> list[dict[str, object]]:
        raise AssertionError(agent_id)

    def ensure_sandbox_metadata(self) -> dict[str, object]:
        raise AssertionError

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        del channels
        return iter(())

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Generator[FiniteFeed]:
        del channels, timeout_s
        assert self.close_calls == 0
        try:
            yield FiniteFeed()
        finally:
            assert self.close_calls == 0


class ErrorAskManager(LifecycleManager):
    @override
    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        del agent_id, text, timeout_s, idle_s, on_chunk
        assert self.close_calls == 0
        raise httpx.ReadError("upstream disconnected")


class DisconnectManager(LifecycleManager):
    def __init__(self) -> None:
        super().__init__()
        self.closed: Event = Event()

    @override
    def close(self) -> None:
        super().close()
        self.closed.set()

    @override
    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        del agent_id, text, timeout_s, idle_s
        assert self.close_calls == 0
        assert on_chunk is not None
        on_chunk("part")
        if not self.closed.wait(1.0):
            raise AssertionError("disconnect did not close the manager")
        return "reply"


class StaticStore:
    def __init__(self) -> None:
        self.record: ShareRecord = ShareRecord("alice", "bot-id", "Shared Bot")

    def authenticate(self, token: str) -> ShareRecord | None:
        return self.record if token == "share-token" else None


def auth() -> dict[str, str]:
    return {"authorization": "Bearer share-token"}


def test_ask_stream_closes_manager_once_after_completion() -> None:
    # Given
    manager = LifecycleManager()
    app = create_share_app(lambda: manager, StaticStore())

    # When
    response = TestClient(app).post(
        "/v1/ask/stream", headers=auth(), json={"text": "hello"}
    )

    # Then
    assert response.status_code == 200
    assert 'event: done\ndata: {"reply": "reply"}' in response.text
    assert manager.close_calls == 1


def test_event_stream_closes_manager_once_after_completion() -> None:
    # Given
    manager = LifecycleManager()
    app = create_share_app(lambda: manager, StaticStore())

    # When
    response = TestClient(app).get("/v1/events", headers=auth())

    # Then
    assert response.status_code == 200
    assert 'event: agent-update\ndata: {"agentId": "bot-id"}' in response.text
    assert manager.close_calls == 1


def test_ask_stream_closes_manager_once_after_upstream_error() -> None:
    # Given
    manager = ErrorAskManager()
    app = create_share_app(lambda: manager, StaticStore())

    # When
    response = TestClient(app).post(
        "/v1/ask/stream", headers=auth(), json={"text": "hello"}
    )

    # Then
    assert response.status_code == 200
    assert 'event: error\ndata: {"detail": "upstream stream failed"}' in response.text
    assert manager.close_calls == 1


@pytest.mark.anyio
async def test_ask_stream_closes_manager_once_when_consumer_disconnects() -> None:
    # Given
    manager = DisconnectManager()
    app = create_share_app(lambda: manager, StaticStore())
    disconnected = anyio.Event()
    request_sent = False
    request_body = json.dumps({"text": "hello"}).encode()

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            disconnected.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/ask/stream",
        "raw_path": b"/v1/ask/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", b"Bearer share-token"),
            (b"content-type", b"application/json"),
        ],
        "client": ("test", 123),
        "server": ("testserver", 80),
    }

    # When
    await app(scope, receive, send)

    # Then
    assert manager.close_calls == 1


class CountingClient(httpx.Client):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls: int = 0

    @override
    def close(self) -> None:
        self.close_calls += 1
        super().close()


def test_relay_manager_closes_owned_http_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    owned_http = CountingClient()

    def make_http(*, timeout: httpx.Timeout) -> httpx.Client:
        del timeout
        return owned_http

    monkeypatch.setattr(httpx, "Client", make_http)
    manager: RelayManager = RelayManager(
        ShareLink("https://relay.example.test", "share-token")
    )

    # When
    manager.close()
    manager.close()

    # Then
    assert owned_http.close_calls == 1
    assert owned_http.is_closed


def test_relay_manager_never_closes_injected_http_client() -> None:
    # Given
    injected_http = CountingClient()
    manager: RelayManager = RelayManager(
        ShareLink("https://relay.example.test", "share-token"), http=injected_http
    )

    try:
        # When
        manager.close()
        manager.close()

        # Then
        assert injected_http.close_calls == 0
        assert not injected_http.is_closed
    finally:
        injected_http.close()
