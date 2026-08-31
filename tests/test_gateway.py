import json
from collections.abc import Callable
from typing import TypedDict, cast, final

import httpx
import pytest
from typing_extensions import override

import groken.gateway as gw_mod
from groken.client import ConnectError
from groken.gateway import GatewayManager, GatewaySession

Handler = Callable[[httpx.Request], httpx.Response]


class RequestCapture(TypedDict):
    url: str
    headers: dict[str, str]
    body: dict[str, object]


def decode_object(content: bytes) -> dict[str, object]:
    return cast(dict[str, object], json.loads(content))


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


def test_command_headers_and_url() -> None:
    seen: RequestCapture = {"url": "", "headers": {}, "body": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = decode_object(request.content)
        return httpx.Response(200, json=[{"id": "a1", "name": "알림이"}])

    s = make_session(handler)
    agents = s.list_agents()
    assert agents[0]["name"] == "알림이"
    assert seen["url"] == "https://gw.example/api/listAgents"
    h = seen["headers"]
    assert h["authorization"] == "Bearer gt"
    assert h["x-anyrun-network-token"] == "nt"
    assert seen["body"] == {}


def test_send_prompt_shape_and_nonce() -> None:
    seen: RequestCapture = {"url": "", "headers": {}, "body": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = decode_object(request.content)
        return httpx.Response(200, json={"accepted": True})

    s = make_session(handler)
    out = s.send_prompt("a1", "hello", client_nonce="nonce-1")
    assert out == {"accepted": True}
    assert seen["body"] == {
        "agentId": "a1",
        "prompt": "hello",
        "clientNonce": "nonce-1",
    }


def test_send_prompt_generates_nonce() -> None:
    seen: RequestCapture = {"url": "", "headers": {}, "body": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = decode_object(request.content)
        return httpx.Response(200, json={"accepted": True})

    s = make_session(handler)
    _ = s.send_prompt("a1", "hello")
    assert seen["body"]["clientNonce"]


def test_transcript_tail_uses_id_field() -> None:
    seen: RequestCapture = {"url": "", "headers": {}, "body": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = decode_object(request.content)
        return httpx.Response(
            200, json={"entries": [{"kind": "send-message", "id": "t1"}]}
        )

    s = make_session(handler)
    entries = s.transcript_tail("a1")
    assert seen["body"] == {"id": "a1", "limit": 100}
    assert entries[0]["id"] == "t1"


def test_ask_collects_reply_after_send() -> None:
    @final
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = FakeClock()
    calls = {"tail": 0}

    def handler(request: httpx.Request) -> httpx.Response:
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
        calls["tail"] += 1
        if calls["tail"] == 1:
            entries = [
                {"kind": "send-message", "id": "old", "message": {"content": "earlier"}}
            ]
        else:
            entries = [
                {
                    "kind": "send-message",
                    "id": "old",
                    "message": {"content": "earlier"},
                },
                {"kind": "message", "id": "u1", "role": "user"},
                {"kind": "send-message", "id": "r1", "message": {"content": "441"}},
            ]
        return httpx.Response(200, json={"entries": entries})

    session = make_session(handler, monotonic=clock.monotonic, sleeper=clock.sleep)
    reply = session.ask("a1", "run it", timeout_s=30, idle_s=30)
    assert reply == "441"


def test_manager_closes_replaced_and_current_http_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first = make_session(lambda _request: httpx.Response(200))
    replacement = make_session(lambda _request: httpx.Response(200))
    manager = GatewayManager.__new__(GatewayManager)
    manager.http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    manager.__dict__["_session"] = first
    monkeypatch.setattr(
        manager,
        "_ensure_sandbox",
        lambda: {
            "gatewayUrl": "https://gw2.example",
            "gatewayToken": "gt2",
            "networkToken": "nt2",
            "podId": "pod-2",
        },
    )
    def replacement_factory(
        *,
        gateway_url: str,
        gateway_token: str,
        network_token: str,
        pod_id: str,
    ) -> GatewaySession:
        del gateway_url, gateway_token, network_token, pod_id
        return replacement

    monkeypatch.setattr(gw_mod, "GatewaySession", replacement_factory)

    # When
    selected = manager.session(force=True)

    # Then
    assert selected is replacement
    assert first.http.is_closed

    # When
    manager.close()

    # Then
    assert replacement.http.is_closed
    assert manager.http.is_closed


def test_manager_remints_session_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensured = {"n": 0}

    @final
    class FakeSession(GatewaySession):
        def __init__(self, fail: bool) -> None:
            super().__init__("https://gw.example", "gt", "nt", "pod-1")
            self.fail = fail

        @override
        def command(self, method: str, args: dict[str, object] | None = None) -> object:
            _ = (method, args)
            if self.fail:
                raise ConnectError(401, "expired")
            return ["ok"]

    def fake_session(
        *,
        gateway_url: str,
        gateway_token: str,
        network_token: str,
        pod_id: str,
    ) -> GatewaySession:
        _ = (gateway_url, gateway_token, network_token, pod_id)
        return FakeSession(fail=False)

    mgr = GatewayManager.__new__(GatewayManager)
    mgr.access_token = "t"
    mgr.machine_id = "m"
    mgr.client_version = "0.20.0"
    mgr.http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    mgr.__dict__["_session"] = FakeSession(fail=True)

    def fake_ensure() -> dict[str, object]:
        ensured["n"] += 1
        return {
            "gatewayUrl": "https://gw2",
            "gatewayToken": "gt2",
            "networkToken": "nt2",
            "podId": "p2",
        }

    monkeypatch.setattr(mgr, "_ensure_sandbox", fake_ensure)
    monkeypatch.setattr(gw_mod, "GatewaySession", fake_session)
    assert mgr.command("listAgents") == ["ok"]
    assert ensured["n"] == 1


def test_ensure_sandbox_metadata_delegates_once_and_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    metadata = {
        "execDaemonUrl": "https://exec.example",
        "networkToken": "network-token",
        "execDaemonAuthToken": "auth-token",
        "podId": "pod-1",
    }
    manager = GatewayManager.__new__(GatewayManager)

    def fake_ensure() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return metadata

    monkeypatch.setattr(manager, "_ensure_sandbox", fake_ensure)
    assert manager.ensure_sandbox_metadata() == metadata
    assert calls == 1

    monkeypatch.setattr(manager, "_ensure_sandbox", lambda: {"podId": "pod-1"})
    with pytest.raises(ConnectError, match="missing sandbox metadata"):
        _ = manager.ensure_sandbox_metadata()


def test_ensure_sandbox_refreshes_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, text="expired")

    manager = GatewayManager.__new__(GatewayManager)
    manager.access_token = "expired"
    manager.machine_id = "machine"
    manager.client_version = "0.23.0"
    manager.http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gw_mod, "load_tokens", lambda: {"refreshToken": "refresh"})

    def refresh_tokens(_token: str) -> dict[str, str]:
        return {"accessToken": "still-invalid", "refreshToken": "refresh"}

    monkeypatch.setattr(gw_mod, "refresh_tokens", refresh_tokens)

    with pytest.raises(ConnectError, match="unauthorized after refresh"):
        _ = manager.ensure_sandbox_metadata()

    assert requests == 2
