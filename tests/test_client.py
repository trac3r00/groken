import json

import httpx

import groken.client as client_mod
from groken import config
from groken.client import SandClient


def make_client(handler, monkeypatch, tokens=None):
    monkeypatch.setattr(client_mod, "get_access_token", lambda: "tok-1")
    monkeypatch.setattr(client_mod, "get_machine_id", lambda: "mid-1")
    monkeypatch.setattr(client_mod, "load_tokens", lambda: tokens)
    c = SandClient.__new__(SandClient)
    c.access_token = "tok-1"
    c.machine_id = "mid-1"
    c.client_version = "0.20.0"
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_unary_headers_and_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"sandBoxes": [{"id": "sb-1"}]})

    c = make_client(handler, monkeypatch)
    resp = c.list_sandboxes()
    assert resp["sandBoxes"][0]["id"] == "sb-1"
    assert seen["url"].endswith("/aiserver.v1.GrokBotService/ListSandBoxes")
    h = seen["headers"]
    assert h["authorization"] == "Bearer tok-1"
    assert h["x-cursor-client-type"] == "sand"
    assert h["x-cursor-client-version"] == "0.20.0"
    assert h["x-sand-box-namespace"] == "prod"
    assert h["connect-protocol-version"] == "1"
    assert h["x-cursor-checksum"].endswith("mid-1")
    assert seen["body"] == {}


def test_plugin_discovery_uses_dashboard_rpc(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"servers": []})

    client = make_client(handler, monkeypatch)
    assert client.list_sand_mcp_tools(["user-Gmail"]) == {"servers": []}
    assert seen == {
        "url": "https://api2.cursor.sh/aiserver.v1.DashboardService/ListSandMcpTools",
        "body": {"serverIdentifiers": ["user-Gmail"]},
    }


def test_plugin_execution_uses_structured_dashboard_payload(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"result": {"case": "success", "value": {"content": []}}}})

    client = make_client(handler, monkeypatch)
    result = client.execute_sand_mcp_tool(
        server_identifier="user-X",
        tool_name="search",
        arguments={"query": "grok"},
        tool_call_id="call-1",
        agent_id="agent-1",
    )

    assert result["result"]["result"]["case"] == "success"
    assert seen == {
        "url": "https://api2.cursor.sh/aiserver.v1.DashboardService/ExecuteSandMcpTool",
        "body": {
            "serverIdentifier": "user-X",
            "toolName": "search",
            "args": {"query": "grok"},
            "toolCallId": "call-1",
            "agentId": "agent-1",
        },
    }


def test_401_triggers_single_refresh(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"ok": True})

    refreshed = {"accessToken": "tok-2"}
    monkeypatch.setattr(client_mod, "refresh_tokens", lambda rt: refreshed)
    c = make_client(handler, monkeypatch, tokens={"refreshToken": "rt-1"})
    assert c.unary("svc", "Method", {}) == {"ok": True}
    assert calls == ["Bearer tok-1", "Bearer tok-2"]
    assert c.access_token == "tok-2"


def test_detect_client_version_override(monkeypatch):
    monkeypatch.setenv("SAND_CLIENT_VERSION", "9.9.9")
    assert client_mod.detect_client_version() == "9.9.9"


def test_detect_client_version_falls_back_on_unreadable_plist(monkeypatch, tmp_path):
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(client_mod, "APP_PATH", tmp_path / "missing.plist")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    assert client_mod.detect_client_version() == "0.20.0"


def test_detect_client_version_falls_back_on_malformed_plist(monkeypatch, tmp_path):
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    bad = tmp_path / "Info.plist"
    bad.write_bytes(b"not a plist at all")
    monkeypatch.setattr(client_mod, "APP_PATH", bad)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    assert client_mod.detect_client_version() == "0.20.0"


def test_detect_client_version_reads_plist(monkeypatch, tmp_path):
    import plistlib

    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    good = tmp_path / "Info.plist"
    good.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.2.3"}))
    monkeypatch.setattr(client_mod, "APP_PATH", good)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    assert client_mod.detect_client_version() == "1.2.3"
    assert json.loads((tmp_path / "config.json").read_text())["client_version"] == "1.2.3"


def test_detect_client_version_uses_cached_version_with_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(client_mod, "APP_PATH", tmp_path / "missing.plist")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_config({"client_version": "7.8.9"})

    assert client_mod.detect_client_version() == "7.8.9"
    assert "using cached client version" in capsys.readouterr().err
