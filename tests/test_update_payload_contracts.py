from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext

import pytest

from groken.bot_update import (
    GatewayUpdateBackend,
    RestorePlan,
    UpdateAvailability,
    UpdateIndeterminateError,
    UpdateKind,
    UpdateManifest,
    UpdateOptions,
    UpdateReadinessError,
    UpdateRuntime,
    run_update,
)
from groken.env_restore import RestoreReport
from groken.routines import RoutineEvent
from groken.update_backend import select_update_kind


class EventFeed:
    def __init__(self, events: Iterator[dict[str, object]]) -> None:
        self.events: Iterator[dict[str, object]] = events

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del timeout_s, hold
        return next(self.events)

    def resume(self) -> None:
        return None


class Gateway:
    def __init__(self, response: object) -> None:
        self.response: object = response
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.feed: EventFeed = EventFeed(iter((ready_event(),)))

    def resolve_agent(self, bot: str | None = None) -> str:
        return bot or "bot-1"

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        self.calls.append((method, args))
        return {}

    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object:
        self.calls.append((method, args))
        return self.response

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Generator[EventFeed, None, None]:
        del channels, timeout_s
        yield self.feed

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        raise AssertionError((agent_id, text, timeout_s))


def ready_event() -> dict[str, object]:
    return {
        "event": "message",
        "data": {
            "channel": "forever-box",
            "payload": {
                "agentId": "bot-1",
                "state": "running",
                "vncUrl": "wss://ready",
            },
        },
    }


def test_image_update_uses_verified_forever_box_payload() -> None:
    # Given
    gateway = Gateway({"state": "updating"})
    backend = GatewayUpdateBackend(gateway)

    # When
    with backend.subscribe("bot-1", 10):
        backend.trigger("bot-1", UpdateKind.IMAGE)

    # Then
    assert gateway.calls == [("updateForeverBox", {"id": "bot-1"})]


def test_host_update_uses_official_ui_force_payload_without_box_subscription() -> None:
    # Given
    gateway = Gateway({"started": True})
    backend = GatewayUpdateBackend(gateway)

    # When
    backend.trigger("bot-1", UpdateKind.HOST)

    # Then
    assert gateway.calls == [("updateHostNow", {"force": True})]


def test_host_update_is_one_shot_per_backend() -> None:
    # Given
    gateway = Gateway({"started": True})
    backend = GatewayUpdateBackend(gateway)

    # When
    backend.trigger("bot-1", UpdateKind.HOST)

    # Then
    with pytest.raises(UpdateIndeterminateError):
        backend.trigger("bot-1", UpdateKind.HOST)
    assert gateway.calls == [("updateHostNow", {"force": True})]


def test_host_update_requires_started_reply() -> None:
    # Given
    backend = GatewayUpdateBackend(Gateway({"started": False}))

    # When / Then
    with pytest.raises(UpdateReadinessError, match="did not start"):
        backend.trigger("bot-1", UpdateKind.HOST)


class HostBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, bot: str | None) -> str:
        self.calls.append(f"resolve:{bot}")
        return "bot-1"

    def availability(self, bot_id: str) -> UpdateAvailability:
        self.calls.append(f"availability:{bot_id}")
        return UpdateAvailability(host=True, image=False)

    def subscribe(self, bot_id: str, timeout_s: float) -> AbstractContextManager[None]:
        self.calls.append(f"subscribe:{bot_id}:{timeout_s}")
        return nullcontext()

    def trigger(self, bot_id: str, kind: UpdateKind) -> None:
        self.calls.append(f"trigger:{bot_id}:{kind.value}")

    def wait_ready(self, bot_id: str) -> None:
        raise AssertionError(bot_id)


class HostRuntimeDependency:
    def ensure(self, bot_id: str, *, skip_capture: bool) -> UpdateManifest:
        raise AssertionError((bot_id, skip_capture))

    def run(self, event: RoutineEvent) -> tuple[str, ...]:
        return (event.value,)

    def plan(self, bot_id: str, manifest: UpdateManifest) -> RestorePlan:
        raise AssertionError((bot_id, manifest))

    def restore(self, bot_id: str, manifest: UpdateManifest) -> RestoreReport:
        raise AssertionError((bot_id, manifest))


class Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def prompt(self, message: str) -> str | None:
        raise AssertionError(message)


def test_host_update_has_separate_output_and_skips_image_readiness() -> None:
    # Given
    backend = HostBackend()
    dependency = HostRuntimeDependency()
    console = Console()
    runtime = UpdateRuntime(backend, dependency, dependency, dependency)

    # When
    run_update(UpdateOptions("Demo", True, False), runtime, console)

    # Then
    assert backend.calls == [
        "resolve:Demo",
        "availability:bot-1",
        "trigger:bot-1:host",
    ]
    assert '"selectedUpdate": "host"' in console.lines[0]
    assert console.lines[-1] == "update=host-started"


def test_update_selection_matches_official_ui_image_precedence() -> None:
    # Given / When / Then
    assert (
        select_update_kind(UpdateAvailability(host=True, image=True))
        is UpdateKind.IMAGE
    )
    assert (
        select_update_kind(UpdateAvailability(host=True, image=False))
        is UpdateKind.HOST
    )
    assert (
        select_update_kind(UpdateAvailability(host=True, image=None)) is UpdateKind.HOST
    )
    assert select_update_kind(UpdateAvailability(host=False, image=False)) is None
