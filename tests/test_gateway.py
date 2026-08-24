import json

import httpx
import pytest

import groken.gateway as gw_mod
from groken.gateway import GatewayManager, GatewaySession


def make_session(handler):
    s = GatewaySession.__new__(GatewaySession)
    s.gateway_url = "https://gw.example"
    s.gateway_token = "gt"
    s.network_token = "nt"
    s.pod_id = "pod-1"
    s.http = httpx.Client(transport=httpx.MockTransport(handler))
    return s


def test_command_headers_and_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"id": "a1", "name": "알림이"}])

    s = make_session(handler)
    agents = s.list_agents()
    assert agents[0]["name"] == "알림이"
    assert seen["url"] == "https://gw.example/api/listAgents"
    h = seen["headers"]
    assert h["authorization"] == "Bearer gt"
    assert h["x-anyrun-network-token"] == "nt"
    assert seen["body"] == {}


def test_send_prompt_shape_and_nonce():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"accepted": True})

    s = make_session(handler)
    out = s.send_prompt("a1", "hello", client_nonce="nonce-1")
    assert out == {"accepted": True}
    assert seen["body"] == {"agentId": "a1", "prompt": "hello", "clientNonce": "nonce-1"}


def test_send_prompt_generates_nonce():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"accepted": True})

    s = make_session(handler)
    s.send_prompt("a1", "hello")
    assert seen["body"]["clientNonce"]


def test_transcript_tail_uses_id_field():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"entries": [{"kind": "send-message", "id": "t1"}]})

    s = make_session(handler)
    entries = s.transcript_tail("a1")
    assert seen["body"] == {"id": "a1"}
    assert entries[0]["id"] == "t1"


def test_ask_collects_reply_after_send():
    calls = {"tail": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/sendPrompt"):
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/events"):
            return httpx.Response(404, text="no events in poll-path test")
        if request.url.path.endswith("/api/listAgents"):
            return httpx.Response(404, text="no roster in poll-path test")
        calls["tail"] += 1
        if calls["tail"] == 1:
            entries = [{"kind": "send-message", "id": "old", "message": {"content": "earlier"}}]
        else:
            entries = [
                {"kind": "send-message", "id": "old", "message": {"content": "earlier"}},
                {"kind": "message", "id": "u1", "role": "user"},
                {"kind": "send-message", "id": "r1", "message": {"content": "441"}},
            ]
        return httpx.Response(200, json={"entries": entries})

    s = make_session(handler)
    import groken.gateway as g
    orig_sleep = g.time.sleep
    g.time.sleep = lambda _s: None
    try:
        reply = s.ask("a1", "run it", timeout_s=30, idle_s=0)
    finally:
        g.time.sleep = orig_sleep
    assert reply == "441"


def test_manager_remints_session_on_failure(monkeypatch):
    ensured = {"n": 0}

    class FakeSession:
        def __init__(self, fail):
            self.fail = fail

        def command(self, method, args=None):
            if self.fail:
                from groken.client import ConnectError
                raise ConnectError(401, "expired")
            return ["ok"]

    mgr = GatewayManager.__new__(GatewayManager)
    mgr.access_token = "t"
    mgr.machine_id = "m"
    mgr.client_version = "0.20.0"
    mgr.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    mgr.__dict__["_session"] = FakeSession(fail=True)

    def fake_ensure():
        ensured["n"] += 1
        return {"gatewayUrl": "https://gw2", "gatewayToken": "gt2", "networkToken": "nt2", "podId": "p2"}

    monkeypatch.setattr(GatewayManager, "_ensure_sandbox", lambda self: fake_ensure())
    monkeypatch.setattr(gw_mod, "GatewaySession", lambda **kw: FakeSession(fail=False))
    assert mgr.command("listAgents") == ["ok"]
    assert ensured["n"] == 1


def test_ensure_sandbox_metadata_delegates_once_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
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
    with pytest.raises(gw_mod.ConnectError, match="missing sandbox metadata"):
        manager.ensure_sandbox_metadata()


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
    manager._session = None
    monkeypatch.setattr(gw_mod, "load_tokens", lambda: {"refreshToken": "refresh"})
    monkeypatch.setattr(
        gw_mod,
        "refresh_tokens",
        lambda _token: {"accessToken": "still-invalid", "refreshToken": "refresh"},
    )

    with pytest.raises(gw_mod.ConnectError, match="unauthorized after refresh"):
        manager._ensure_sandbox()

    assert requests == 2
