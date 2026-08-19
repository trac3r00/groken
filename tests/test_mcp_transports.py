import sys

import groken.mcp_server as m


def test_main_supports_transport_flag(monkeypatch):
    seen = {}

    async def fake_http(self, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(m.MCPServer, "run_streamable_http_async", fake_http)
    monkeypatch.setattr(m, "asyncio", __import__("asyncio"))
    monkeypatch.setattr(sys, "argv", ["groken-mcp", "--transport", "http", "--port", "9123", "--host", "0.0.0.0"])
    m.main()
    assert seen.get("port") == 9123
    assert seen.get("host") == "0.0.0.0"


def test_main_defaults_to_stdio(monkeypatch):
    seen = {}

    async def fake_stdio(self):
        seen["stdio"] = True

    monkeypatch.setattr(m.MCPServer, "run_stdio_async", fake_stdio)
    monkeypatch.setattr(sys, "argv", ["groken-mcp"])
    m.main()
    assert seen.get("stdio")


def test_main_supports_sse(monkeypatch):
    seen = {}

    async def fake_sse(self, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(m.MCPServer, "run_sse_async", fake_sse)
    monkeypatch.setattr(sys, "argv", ["groken-mcp", "--transport", "sse", "--port", "9222"])
    m.main()
    assert seen.get("port") == 9222
