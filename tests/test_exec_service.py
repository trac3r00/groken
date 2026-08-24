import asyncio
import json
import struct

import httpx
import pytest

from groken.exec_service import ExecProtocolError, ExecResult, ExecServiceClient


class Manager:
    def __init__(self):
        self.calls = 0
    def ensure_sandbox_metadata(self):
        self.calls += 1
        return {"execDaemonUrl": "https://exec.test", "networkToken": "nt", "execDaemonAuthToken": "at", "podId": "p"}


def frame(value, flags=0):
    raw = json.dumps(value).encode()
    return struct.pack(">BI", flags, len(raw)) + raw


def test_exec_connect_request_and_split_response():
    manager = Manager()
    seen = []
    async def handler(request):
        seen.append(request)
        return httpx.Response(200, content=frame({"exec_client_message": {"shell_result": {"success": {"stdout": "ok"}}}}) + frame({}, 128))
    client = ExecServiceClient(manager, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(client.execute("printf ok"))
    assert result == ExecResult("ok", "")
    assert str(seen[0].url) == "https://exec.test/agent.v1.ExecService/Exec?network_token=nt"
    assert seen[0].headers["authorization"] == "Bearer at"
    assert seen[0].headers["content-type"] == "application/connect+json"
    assert json.loads(seen[0].content[5:]) ["shell_args"]["command"] == "printf ok"


def test_camel_case_live_response_and_stream_close():
    live_stdout = "Linux cursor 6.12.94+ #1 SMP PREEMPT_DYNAMIC Tue Jul 28 22:00:48 UTC 2026 x86_64 GNU/Linux\\n"

    async def handler(request):
        return httpx.Response(200, content=(
            frame({"execClientMessage": {"id": 1, "shellResult": {"success": {
                "command": "uname -a", "workingDirectory": "/workspace", "stdout": live_stdout,
            }}}})
            + frame({"execClientControlMessage": {"streamClose": {"id": 1}}})
            + frame({"execClientMessage": {"shellResult": {"failure": {"exit_code": 1, "stderr": "nope"}}}})
        ))

    client = ExecServiceClient(Manager(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(client.execute("uname -a")) == ExecResult(live_stdout, "nope")


def test_malformed_or_failure_envelopes_fail_closed():
    async def handler(request):
        return httpx.Response(200, content=(
            frame({"exec_client_message": {"shell_result": {"success": {"stdout": "bad"}}},
                   "execClientMessage": {"shellResult": {"success": {"stdout": "bad2"}}}})
            + frame({})
        ))

    client = ExecServiceClient(Manager(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(client._call("https://exec.test", "nt", {}, b"")) == (ExecResult("", ""), False)


def test_truncated_response_is_protocol_error():
    async def handler(request):
        return httpx.Response(200, content=struct.pack(">BI", 0, 20) + b"{}");
    client = ExecServiceClient(Manager(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ExecProtocolError):
        asyncio.run(client.execute("true"))
