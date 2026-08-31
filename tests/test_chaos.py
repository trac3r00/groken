import json
from collections.abc import Callable
from typing import Protocol, cast, final

import httpx
import pytest
from typing_extensions import override

from groken.client import ConnectError
from groken.gateway import GatewayManager, GatewaySession

Handler = Callable[[httpx.Request], httpx.Response]


class _ManagerCommand(Protocol):
    def command(
        self, method: str, args: dict[str, object] | None = None
    ) -> object: ...


@final
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_session(
    handler: Handler,
    *,
    monotonic: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> GatewaySession:
    session = GatewaySession(
        "https://gw.example",
        "gt",
        "nt",
        "pod-1",
        monotonic=monotonic,
        sleeper=sleeper,
    )
    session.http = httpx.Client(transport=httpx.MockTransport(handler))
    return session


def test_send_prompt_server_500_raises_connect_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    session = make_session(handler)
    with pytest.raises(ConnectError):
        _ = session.send_prompt("a1", "hi")


def test_tail_malformed_entries_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    session = make_session(handler)
    assert session.transcript_tail("a1") == []


def test_tail_null_body_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    session = make_session(handler)
    assert session.transcript_tail("a1") == []


def test_ask_dedupes_same_entry_across_polls() -> None:
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/api/sendPrompt"):
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/events"):
            return httpx.Response(404, text="no events in poll-path test")
        if request.url.path.endswith("/api/listAgents"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "a1",
                        "isComposingMessage": False,
                        "isRunning": False,
                    }
                ],
            )
        calls += 1
        entries: list[dict[str, object]] = [
            {
                "kind": "send-message",
                "id": "old",
                "message": {"content": "earlier"},
            }
        ]
        if calls >= 2:
            entries.append(
                {
                    "kind": "send-message",
                    "id": "r1",
                    "message": {"content": "done"},
                }
            )
        return httpx.Response(200, json={"entries": entries})

    session = make_session(
        handler, monotonic=clock.monotonic, sleeper=clock.sleep
    )
    reply = session.ask("a1", "q", timeout_s=30, idle_s=30)
    assert reply == "done"
    assert reply.count("done") == 1


def test_unicode_roundtrip_body() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = cast(dict[str, object], json.loads(request.content))
        bodies.append(parsed)
        return httpx.Response(200, json={"accepted": True})

    session = make_session(handler)
    _ = session.send_prompt("a1", "한글 + emoji 🚀 + 中文")
    assert len(bodies) == 1
    assert bodies[0]["prompt"] == "한글 + emoji 🚀 + 中文"


def test_manager_double_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = GatewayManager.__new__(GatewayManager)
    manager.access_token = "t"
    manager.machine_id = "m"
    manager.client_version = "0.20.0"
    manager.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, text="down")
        )
    )

    @final
    class FailingSession:
        def command(
            self, method: str, args: dict[str, object] | None = None
        ) -> object:
            assert method == "listAgents"
            assert args is None
            raise ConnectError(500, "down")

    def ensure_sandbox() -> dict[str, str]:
        return {
            "gatewayUrl": "x",
            "gatewayToken": "y",
            "networkToken": "z",
        }

    def make_failing_session(
        *,
        gateway_url: str,
        gateway_token: str,
        network_token: str,
        pod_id: str,
    ) -> FailingSession:
        assert (gateway_url, gateway_token, network_token, pod_id) == (
            "x",
            "y",
            "z",
            "",
        )
        return FailingSession()

    manager.__dict__["_session"] = None
    monkeypatch.setattr(manager, "_ensure_sandbox", ensure_sandbox)
    monkeypatch.setattr("groken.gateway.GatewaySession", make_failing_session)
    typed_manager = cast(_ManagerCommand, manager)
    with pytest.raises(ConnectError):
        _ = typed_manager.command("listAgents")


def test_own_agent_create_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("groken.gateway.cached_bot_id", lambda: None)
    monkeypatch.setattr("groken.gateway.bot_name", lambda: "groken")

    def remember_bot(_identifier: str, _name: str) -> None:
        return None

    monkeypatch.setattr("groken.gateway.remember_bot", remember_bot)

    @final
    class CreateFailingManager(GatewayManager):
        @override
        def command(
            self, method: str, args: dict[str, object] | None = None
        ) -> object:
            if method == "createAgent":
                assert args is not None
                raise ConnectError(500, "quota")
            assert method == "listAgents"
            assert args is None
            return []

    manager = CreateFailingManager.__new__(CreateFailingManager)
    with pytest.raises(ConnectError):
        _ = manager.own_agent_id()


def test_ask_detects_completion_via_roster_compose_flip_not_idle() -> None:
    composing = True

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/sendPrompt"):
            return httpx.Response(200, json={"accepted": True})
        if path.endswith("/api/getAgentTranscriptTail"):
            entries: list[dict[str, object]] = [
                {
                    "kind": "send-message",
                    "id": "old",
                    "message": {"content": "earlier"},
                }
            ]
            if not composing:
                entries.append(
                    {
                        "kind": "send-message",
                        "id": "r1",
                        "message": {"content": "final answer"},
                    }
                )
            return httpx.Response(200, json={"entries": entries})
        if path.endswith("/api/listAgents"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "a1",
                        "name": "groken",
                        "isComposingMessage": composing,
                        "isRunning": composing,
                    }
                ],
            )
        return httpx.Response(404)

    clock = FakeClock()
    polls = 0

    def fake_sleep(seconds: float) -> None:
        nonlocal composing, polls
        clock.sleep(seconds)
        polls += 1
        if polls >= 3:
            composing = False

    session = make_session(
        handler, monotonic=clock.monotonic, sleeper=fake_sleep
    )
    reply = session.ask("a1", "q", timeout_s=1000, idle_s=1000)
    assert "final answer" in reply
    assert polls < 20, (
        f"took {polls} polls — completion detected by timeout, not roster flip"
    )
