import asyncio
import sys

import pytest
from mcp.server.mcpserver import MCPServer

import groken.mcp_server as m


def test_main_supports_transport_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str | int] = {}

    async def fake_http(_server: MCPServer, *, host: str, port: int) -> None:
        seen.update(host=host, port=port)

    monkeypatch.setattr(MCPServer, "run_streamable_http_async", fake_http)
    monkeypatch.setattr(m, "asyncio", asyncio)
    monkeypatch.setattr(sys, "argv", ["groken-mcp", "--transport", "http", "--port", "9123", "--host", "0.0.0.0"])
    m.main()
    assert seen.get("port") == 9123
    assert seen.get("host") == "0.0.0.0"


def test_main_defaults_to_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bool] = {}

    async def fake_stdio(_server: MCPServer) -> None:
        seen["stdio"] = True

    monkeypatch.setattr(MCPServer, "run_stdio_async", fake_stdio)
    monkeypatch.setattr(sys, "argv", ["groken-mcp"])
    m.main()
    assert seen.get("stdio")


def test_main_supports_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str | int] = {}

    async def fake_sse(_server: MCPServer, *, host: str, port: int) -> None:
        seen.update(host=host, port=port)

    monkeypatch.setattr(MCPServer, "run_sse_async", fake_sse)
    monkeypatch.setattr(sys, "argv", ["groken-mcp", "--transport", "sse", "--port", "9222"])
    m.main()
    assert seen.get("port") == 9222
