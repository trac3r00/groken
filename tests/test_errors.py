import asyncio

import httpx
import pytest

from groken.client import ConnectError
from groken.errors import explain_error


def test_auth_error_suggests_login():
    assert "groken login" in explain_error(ConnectError(401, "unauthenticated"))


def test_unroutable_suggests_retry():
    msg = explain_error(ConnectError(404, "The request could not be routed"))
    assert "sandbox" in msg and ("retry" in msg.lower() or "recover" in msg.lower())


def test_unknown_command_suggests_app_update():
    msg = explain_error(ConnectError(404, "unknown gateway method: foo"))
    assert "Grok Bot" in msg and "updat" in msg.lower()


def test_plain_error_passthrough():
    msg = explain_error(ValueError("unknown bot: nobody"))
    assert "unknown bot" in msg


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (ConnectError(429, "quota exceeded"), "quota"),
        (ConnectError(0, "connection failed"), "network"),
        (httpx.TimeoutException("slow"), "timed out"),
        (httpx.ConnectError("refused"), "connect"),
        (ConnectError(401, "unauthorized after refresh"), "refresh"),
    ],
)
def test_explain_error_actionable_cases(exc, fragment):
    assert fragment in explain_error(exc).lower()


def test_mcp_ask_translates_connect_error(monkeypatch):
    from groken import mcp_server

    class RaisingManager:
        def resolve_agent(self, bot):
            return "agent"

        def ask(self, *args):
            raise ConnectError(429, "quota exceeded")

    monkeypatch.setattr(mcp_server, "GatewayManager", RaisingManager)
    result = asyncio.run(mcp_server.grok_bot_ask("hello"))
    assert result == explain_error(ConnectError(429, "quota exceeded"))
    assert "traceback" not in result.lower()


def test_mcp_does_not_swallow_unexpected_errors(monkeypatch):
    from groken import mcp_server

    class RaisingManager:
        def resolve_agent(self, bot):
            raise ValueError("bad test input")

    monkeypatch.setattr(mcp_server, "GatewayManager", RaisingManager)
    with pytest.raises(ValueError, match="bad test input"):
        asyncio.run(mcp_server.grok_bot_ask("hello"))
