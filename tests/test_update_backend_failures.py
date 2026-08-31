from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import final

import httpx
import pytest

from groken.bot_update import (
    GatewayUpdateBackend,
    UpdateIndeterminateError,
    UpdateKind,
    UpdateReadinessError,
)
from groken.client import ConnectError


@final
class EventFeed:
    def __init__(self, events: Iterator[dict[str, object]]) -> None:
        self.events = events

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del timeout_s, hold
        return next(self.events)

    def resume(self) -> None:
        return None


@final
class Gateway:
    def __init__(
        self,
        events: Iterator[dict[str, object]],
        mutation_error: BaseException | None = None,
    ) -> None:
        self.events: EventFeed = EventFeed(events)
        self.mutation_error: BaseException | None = mutation_error
        self.mutations: int = 0

    def resolve_agent(self, bot: str | None = None) -> str:
        return bot or "bot-1"

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        del method, args
        return {"imageUpdateAvailable": True}

    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object:
        del method, args
        self.mutations += 1
        if self.mutation_error is not None:
            raise self.mutation_error
        return {"state": "updating"}

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Generator[EventFeed, None, None]:
        del channels, timeout_s
        yield self.events

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        raise AssertionError((agent_id, text, timeout_s))


def box_event(state: str, vnc_url: str | None) -> dict[str, object]:
    return {
        "event": "message",
        "data": {
            "channel": "forever-box",
            "payload": {
                "agentId": "bot-1",
                "state": state,
                "vncUrl": vnc_url,
            },
        },
    }


def ready_event() -> dict[str, object]:
    return box_event("running", "wss://ready")


@pytest.mark.parametrize(
    "error", [httpx.ReadError("reset"), ConnectError(500, "semantic")]
)
def test_mutation_failure_is_never_retried(error: BaseException) -> None:
    # Given
    gateway = Gateway(iter((ready_event(),)), error)
    backend = GatewayUpdateBackend(gateway)
    expected = (
        UpdateIndeterminateError
        if isinstance(error, httpx.TransportError)
        else ConnectError
    )

    # When / Then
    with backend.subscribe("bot-1", 1), pytest.raises(expected):
        backend.trigger("bot-1", UpdateKind.IMAGE)
    assert gateway.mutations == 1


@pytest.mark.parametrize(
    "error", [TimeoutError("deadline"), httpx.ReadError("disconnected")]
)
def test_readiness_timeout_or_disconnect_never_reports_success(
    error: BaseException,
) -> None:
    # Given
    def failed_events() -> Iterator[dict[str, object]]:
        yield ready_event()
        raise error

    backend = GatewayUpdateBackend(Gateway(failed_events()))

    # When / Then
    with backend.subscribe("bot-1", 1):
        backend.trigger("bot-1", UpdateKind.IMAGE)
        with pytest.raises(UpdateReadinessError, match="timed out|disconnected"):
            backend.wait_ready("bot-1")
