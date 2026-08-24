import asyncio
import json

import groken.native_mcp_server as m


def test_direct_cloud_exec_is_registered():
    assert "direct_cloud_exec" in m.server._tool_manager._tools


def test_direct_cloud_exec_delegates_to_client(monkeypatch):
    calls = []

    class Result:
        stdout = "ok\n"
        stderr = ""

    class FakeClient:
        async def execute(self, command):
            calls.append(command)
            return Result()

    monkeypatch.setattr(m, "ExecServiceClient", FakeClient)
    result = json.loads(asyncio.run(m.direct_cloud_exec("printf ok")))
    assert result == {"stdout": "ok\n", "stderr": ""}
    assert calls == ["printf ok"]
