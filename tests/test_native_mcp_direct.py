import asyncio
import json
import logging
import sys
from typing import cast, final

import pytest

import groken.native_mcp_server as m


def test_main_suppresses_sensitive_transport_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_levels: list[tuple[int, int]] = []
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_levels = (httpx_logger.level, httpcore_logger.level)

    async def run_stdio_async() -> None:
        observed_levels.append((httpx_logger.level, httpcore_logger.level))

    monkeypatch.setattr(m.server, "run_stdio_async", run_stdio_async)
    monkeypatch.setattr(sys, "argv", ["groken-native-mcp"])
    try:
        m.main()
    finally:
        httpx_logger.setLevel(original_levels[0])
        httpcore_logger.setLevel(original_levels[1])

    assert observed_levels == [(logging.WARNING, logging.WARNING)]


def test_direct_cloud_exec_is_registered() -> None:
    tools = asyncio.run(m.server.list_tools())
    assert "direct_cloud_exec" in {tool.name for tool in tools}


def test_direct_cloud_exec_delegates_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    @final
    class Result:
        stdout: str = "ok\n"
        stderr: str = ""
        exit_code: int = 7

    class FakeClient:
        async def execute(self, command: str) -> Result:
            calls.append(command)
            return Result()

    monkeypatch.setattr(m, "ExecServiceClient", FakeClient)
    result = cast(
        "dict[str, object]", json.loads(asyncio.run(m.direct_cloud_exec("printf ok")))
    )
    assert result == {"stdout": "ok\n", "stderr": "", "exit_code": 7}
    assert calls == ["printf ok"]
