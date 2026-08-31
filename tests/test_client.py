import json
import plistlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest

import groken.client as client_mod
from groken import config
from groken.client import SandClient

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class RequestCapture:
    headers: dict[str, str] | None = None
    url: str | None = None
    body: dict[str, object] | None = None


def decode_object(content: bytes) -> dict[str, object]:
    return cast(dict[str, object], json.loads(content))


def require_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def require_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def make_client(
    handler: Handler,
    monkeypatch: pytest.MonkeyPatch,
    tokens: dict[str, object] | None = None,
) -> SandClient:
    monkeypatch.setattr(client_mod, "get_access_token", lambda: "tok-1")
    monkeypatch.setattr(client_mod, "get_machine_id", lambda: "mid-1")
    monkeypatch.setattr(client_mod, "load_tokens", lambda: tokens)
    client = SandClient.__new__(SandClient)
    client.access_token = "tok-1"
    client.machine_id = "mid-1"
    client.client_version = "0.20.0"
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_unary_headers_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = RequestCapture()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.headers = dict(request.headers)
        seen.url = str(request.url)
        seen.body = decode_object(request.content)
        return httpx.Response(200, json={"sandBoxes": [{"id": "sb-1"}]})

    client = make_client(handler, monkeypatch)
    response = client.list_sandboxes()
    sandboxes = require_list(response["sandBoxes"])
    assert len(sandboxes) == 1
    sandbox = require_object(sandboxes[0])
    assert sandbox["id"] == "sb-1"
    assert seen.url is not None
    assert seen.url.endswith("/aiserver.v1.GrokBotService/ListSandBoxes")
    assert seen.headers is not None
    assert seen.headers["authorization"] == f"Bearer {'tok-1'}"
    assert seen.headers["x-cursor-client-type"] == "sand"
    assert seen.headers["x-cursor-client-version"] == "0.20.0"
    assert seen.headers["x-sand-box-namespace"] == "prod"
    assert seen.headers["connect-protocol-version"] == "1"
    assert seen.headers["x-cursor-checksum"].endswith("mid-1")
    assert seen.body == {}


def test_plugin_discovery_uses_dashboard_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = RequestCapture()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.url = str(request.url)
        seen.body = decode_object(request.content)
        return httpx.Response(200, json={"servers": []})

    client = make_client(handler, monkeypatch)
    assert client.list_sand_mcp_tools(["user-Gmail"]) == {"servers": []}
    assert seen.url == (
        "https://api2.cursor.sh/aiserver.v1.DashboardService/ListSandMcpTools"
    )
    assert seen.body == {"serverIdentifiers": ["user-Gmail"]}


def test_plugin_execution_uses_structured_dashboard_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = RequestCapture()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.url = str(request.url)
        seen.body = decode_object(request.content)
        return httpx.Response(
            200,
            json={"result": {"result": {"case": "success", "value": {"content": []}}}},
        )

    client = make_client(handler, monkeypatch)
    result = client.execute_sand_mcp_tool(
        server_identifier="user-X",
        tool_name="search",
        arguments={"query": "grok"},
        tool_call_id="call-1",
        agent_id="agent-1",
    )

    outer_result = require_object(result["result"])
    inner_result = require_object(outer_result["result"])
    assert inner_result["case"] == "success"
    assert seen.url == (
        "https://api2.cursor.sh/aiserver.v1.DashboardService/ExecuteSandMcpTool"
    )
    assert seen.body == {
        "serverIdentifier": "user-X",
        "toolName": "search",
        "args": {"query": "grok"},
        "toolCallId": "call-1",
        "agentId": "agent-1",
    }


def test_401_triggers_single_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"ok": True})

    refreshed: dict[str, object] = {"accessToken": "tok-2"}

    def refresh(refresh_token: str) -> dict[str, object]:
        assert refresh_token == "rt-1"
        return refreshed

    monkeypatch.setattr(client_mod, "refresh_tokens", refresh)
    client = make_client(handler, monkeypatch, tokens={"refreshToken": "rt-1"})
    assert client.unary("svc", "Method", {}) == {"ok": True}
    assert calls == [f"Bearer {'tok-1'}", f"Bearer {'tok-2'}"]
    assert client.access_token == "tok-2"


def test_detect_client_version_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAND_CLIENT_VERSION", "9.9.9")
    assert client_mod.detect_client_version() == "9.9.9"


def test_detect_client_version_falls_back_on_unreadable_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(client_mod, "APP_PATH", tmp_path / "missing.plist")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    assert client_mod.detect_client_version() == "0.20.0"


def test_detect_client_version_falls_back_on_malformed_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    bad = tmp_path / "Info.plist"
    _ = bad.write_bytes(b"not a plist at all")
    monkeypatch.setattr(client_mod, "APP_PATH", bad)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    assert client_mod.detect_client_version() == "0.20.0"


def test_detect_client_version_uses_outer_bundle_when_embedded_version_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    app = tmp_path / "Grok Bot.app" / "Contents"
    resources = app / "Resources"
    resources.mkdir(parents=True)
    info = app / "Info.plist"
    _ = info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.27.0"}))
    _ = (resources / "package.json").write_text('{"version":"0.24.0"}')
    monkeypatch.setattr(client_mod, "APP_PATH", info)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")

    assert client_mod.detect_client_version() == "0.27.0"


def test_detect_client_version_reads_unrecognized_outer_version_without_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    good = tmp_path / "Info.plist"
    _ = good.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.2.3"}))
    monkeypatch.setattr(client_mod, "APP_PATH", good)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    assert client_mod.detect_client_version() == "1.2.3"
    saved = cast(dict[str, object], json.loads((tmp_path / "config.json").read_text()))
    assert saved["client_version"] == "1.2.3"


def test_detect_client_version_uses_cached_version_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SAND_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(client_mod, "APP_PATH", tmp_path / "missing.plist")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_config({"client_version": "7.8.9"})

    assert client_mod.detect_client_version() == "7.8.9"
    assert "using cached client version" in capsys.readouterr().err
