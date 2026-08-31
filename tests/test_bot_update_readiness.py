from __future__ import annotations

import json
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import final

import httpx
import pytest
from typing_extensions import override

from groken.bot_update import GatewayUpdateBackend, UpdateKind
from groken.gateway import (
    GatewayEventFeed,
    GatewaySession,
    UpdateAvailability,
    UpdateReadinessError,
)


class FakeEventFeed:
    def __init__(self, events: Iterator[dict[str, object]]) -> None:
        self.events: Iterator[dict[str, object]] = events

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del timeout_s, hold
        return next(self.events)

    def resume(self) -> None:
        return None


@final
class FakeGateway:
    def __init__(self, events: Iterator[dict[str, object]]) -> None:
        self.events = FakeEventFeed(events)
        self.log: list[str] = []
        self.mutations: int = 0

    def resolve_agent(self, bot: str | None = None) -> str:
        return bot or "bot-1"

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        del args
        self.log.append(method)
        return {"hostUpdateAvailable": "misleading", "imageUpdateAvailable": True}

    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object:
        del args
        self.mutations += 1
        self.log.append(method)
        return {"state": "updating"}

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Generator[FakeEventFeed, None, None]:
        self.log.append(f"subscribed:{','.join(channels)}:{timeout_s}")
        yield self.events

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        raise AssertionError((agent_id, text, timeout_s))


def box_event(state: str, vnc_url: str | None) -> dict[str, object]:
    return {
        "event": "message",
        "data": {
            "channel": "forever-box",
            "payload": {"agentId": "bot-1", "state": state, "vncUrl": vnc_url},
        },
    }


def ready_event() -> dict[str, object]:
    return box_event("running", "wss://ready")


def sse(event: dict[str, object]) -> bytes:
    return b"event: message\ndata: " + json.dumps(event["data"]).encode() + b"\n\n"


@final
class SessionGateway:
    def __init__(self, session: GatewaySession) -> None:
        self.session: GatewaySession = session
        self.mutations: int = 0

    def resolve_agent(self, bot: str | None = None) -> str:
        return bot or "bot-1"

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        _ = (method, args)
        return {}

    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object:
        _ = (method, args)
        self.mutations += 1
        return {"state": "updating"}

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Generator[GatewayEventFeed, None, None]:
        with self.session.event_subscription(channels, timeout_s) as events:
            yield events

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        raise AssertionError((agent_id, text, timeout_s))


def session_for(stream: httpx.SyncByteStream) -> GatewaySession:
    session = GatewaySession("https://gw.example", "gt", "nt", "pod-1")
    session.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=stream)
        )
    )
    return session


def test_gateway_backend_requires_transition_then_accepts_ready() -> None:
    # Given
    gateway = FakeGateway(
        iter((ready_event(), box_event("absent", None), ready_event()))
    )
    backend = GatewayUpdateBackend(gateway)

    # When
    availability = backend.availability("bot-1")
    with backend.subscribe("bot-1", 9):
        backend.trigger("bot-1", UpdateKind.IMAGE)
        backend.wait_ready("bot-1")

    # Then
    assert availability == UpdateAvailability(host=None, image=True)
    assert gateway.log == [
        "getForeverBoxStatus",
        "subscribed:forever-box:9",
        "updateForeverBox",
    ]
    assert gateway.mutations == 1


def test_pretrigger_ready_baseline_cannot_satisfy_posttrigger_readiness() -> None:
    # Given
    gateway = FakeGateway(iter((ready_event(),)))
    backend = GatewayUpdateBackend(gateway)

    # When / Then
    with backend.subscribe("bot-1", 1):
        backend.trigger("bot-1", UpdateKind.IMAGE)
        with pytest.raises(UpdateReadinessError, match="disconnected"):
            backend.wait_ready("bot-1")
    assert gateway.mutations == 1


def test_immediate_transition_and_ready_frames_do_not_race_trigger() -> None:
    # Given
    frames = sse(ready_event()) + sse(box_event("absent", None)) + sse(ready_event())
    stream = httpx.ByteStream(frames)
    gateway = SessionGateway(session_for(stream))
    backend = GatewayUpdateBackend(gateway)

    # When
    with backend.subscribe("bot-1", 1):
        backend.trigger("bot-1", UpdateKind.IMAGE)
        backend.wait_ready("bot-1")

    # Then
    assert gateway.mutations == 1
    assert not any(
        thread.name.startswith("groken-event-reader")
        for thread in threading.enumerate()
    )


def test_hard_deadline_stops_incomplete_trickle_and_cleans_reader() -> None:
    # Given
    @final
    class TrickleStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.closed: threading.Event = threading.Event()

        @override
        def __iter__(self) -> Iterator[bytes]:
            yield sse(box_event("absent", None))
            while not self.closed.is_set():
                yield b"x"

        @override
        def close(self) -> None:
            self.closed.set()

    stream = TrickleStream()
    backend = GatewayUpdateBackend(SessionGateway(session_for(stream)))
    finished = threading.Event()
    outcome: list[str] = []

    def wait() -> None:
        try:
            with backend.subscribe("bot-1", 0.02):
                backend.trigger("bot-1", UpdateKind.IMAGE)
                backend.wait_ready("bot-1")
        except UpdateReadinessError as exc:
            outcome.append(str(exc))
        finally:
            finished.set()

    worker = threading.Thread(target=wait, name="task5-hard-deadline")

    # When
    worker.start()
    completed_within_bound = finished.wait(0.5)
    if not completed_within_bound:
        stream.close()
    worker.join(timeout=1)

    # Then
    assert completed_within_bound
    assert outcome == ["update readiness timed out"]
    assert stream.closed.is_set()
    assert not worker.is_alive()
    assert not any(
        thread.name.startswith("groken-event-reader")
        for thread in threading.enumerate()
    )
