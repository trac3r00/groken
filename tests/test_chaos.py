import json

import httpx
import pytest

import groken.gateway as gw_mod
from groken.client import ConnectError
from groken.gateway import GatewayManager, GatewaySession


def make_session(handler):
    s = GatewaySession.__new__(GatewaySession)
    s.gateway_url = "https://gw.example"
    s.gateway_token = "gt"
    s.network_token = "nt"
    s.pod_id = "pod-1"
    s.http = httpx.Client(transport=httpx.MockTransport(handler))
    return s


def test_send_prompt_server_500_raises_connect_error():
    s = make_session(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(ConnectError):
        s.send_prompt("a1", "hi")


def test_tail_malformed_entries_returns_empty():
    s = make_session(lambda r: httpx.Response(200, json={"unexpected": True}))
    assert s.transcript_tail("a1") == []


def test_tail_null_body_returns_empty():
    s = make_session(lambda r: httpx.Response(200, text=""))
    assert s.transcript_tail("a1") == []


def test_ask_dedupes_same_entry_across_polls():
    class FakeClock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = FakeClock()
    calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/api/sendPrompt"):
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/events"):
            return httpx.Response(404, text="no events in poll-path test")
        if request.url.path.endswith("/api/listAgents"):
            return httpx.Response(200, json=[{
                "id": "a1", "isComposingMessage": False, "isRunning": False,
            }])
        calls["n"] += 1
        entries = [{"kind": "send-message", "id": "old", "message": {"content": "earlier"}}]
        if calls["n"] >= 2:
            entries.append({"kind": "send-message", "id": "r1", "message": {"content": "done"}})
        return httpx.Response(200, json={"entries": entries})

    s = make_session(handler)
    s._monotonic = clock.monotonic
    s._sleep = clock.sleep
    reply = s.ask("a1", "q", timeout_s=30, idle_s=30)
    assert reply == "done"
    assert reply.count("done") == 1


def test_unicode_roundtrip_body():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"accepted": True})

    s = make_session(handler)
    s.send_prompt("a1", "한글 + emoji 🚀 + 中文")
    assert seen["body"]["prompt"] == "한글 + emoji 🚀 + 中文"


def test_manager_double_failure_propagates():
    mgr = GatewayManager.__new__(GatewayManager)
    mgr.access_token = "t"
    mgr.machine_id = "m"
    mgr.client_version = "0.20.0"
    mgr.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down")))

    class FailingSession:
        def command(self, method, args=None):
            raise ConnectError(500, "down")

    mgr._session = None
    mgr._ensure_sandbox = lambda: {"gatewayUrl": "x", "gatewayToken": "y", "networkToken": "z"}
    gw_mod.GatewaySession = lambda **kw: FailingSession()
    with pytest.raises(ConnectError):
        mgr.command("listAgents")


def test_own_agent_create_failure_propagates(monkeypatch):
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: None)
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")
    monkeypatch.setattr(gw_mod, "remember_bot", lambda i, n: None)
    mgr = GatewayManager.__new__(GatewayManager)

    def boom(method, args=None):
        if method == "createAgent":
            raise ConnectError(500, "quota")
        return []

    mgr.command = boom
    with pytest.raises(ConnectError):
        mgr.own_agent_id()


def test_ask_detects_completion_via_roster_compose_flip_not_idle():
    state = {"composing": True}

    def handler(request):
        path = request.url.path
        if path.endswith("/api/sendPrompt"):
            return httpx.Response(200, json={"accepted": True})
        if path.endswith("/api/getAgentTranscriptTail"):
            entries = [{"kind": "send-message", "id": "old", "message": {"content": "earlier"}}]
            if not state["composing"]:
                entries.append({"kind": "send-message", "id": "r1", "message": {"content": "final answer"}})
            return httpx.Response(200, json={"entries": entries})
        if path.endswith("/api/listAgents"):
            return httpx.Response(200, json=[{"id": "a1", "name": "groken",
                                              "isComposingMessage": state["composing"],
                                              "isRunning": state["composing"]}])
        return httpx.Response(404)

    s = make_session(handler)
    clock = {"t": 0.0}
    polls = {"n": 0}

    def fake_sleep(_s):
        clock["t"] += 1.0
        polls["n"] += 1
        if polls["n"] >= 3:
            state["composing"] = False

    orig_sleep = gw_mod.time.sleep
    orig_mono = gw_mod.time.monotonic
    gw_mod.time.sleep = fake_sleep
    gw_mod.time.monotonic = lambda: clock["t"]
    try:
        reply = s.ask("a1", "q", timeout_s=1000, idle_s=1000)
    finally:
        gw_mod.time.sleep = orig_sleep
        gw_mod.time.monotonic = orig_mono
    assert "final answer" in reply
    assert polls["n"] < 20, f"took {polls['n']} polls — completion detected by timeout, not roster flip"
