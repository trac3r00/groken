import asyncio
import json
import struct
from typing import cast, final

import httpx
import pytest

from groken.exec_service import ExecProtocolError, ExecResult, ExecServiceClient


@final
class Manager:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_sandbox_metadata(self) -> object:
        self.calls += 1
        return {
            "execDaemonUrl": "https://exec.test",
            "networkToken": "nt",
            "execDaemonAuthToken": "at",
            "podId": "p",
        }


def frame(value: object, flags: int = 0) -> bytes:
    raw = json.dumps(value).encode()
    return struct.pack(">BI", flags, len(raw)) + raw


def decode_object(value: str | bytes) -> dict[str, object]:
    return cast(dict[str, object], json.loads(value))


def require_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


class PublicCallExecServiceClient(ExecServiceClient):
    async def call(
        self, url: str, token: str, headers: dict[str, str], body: bytes
    ) -> tuple[ExecResult | None, bool]:
        return await self._call(url, token, headers, body)


def test_owned_http_client_closes_deterministically() -> None:
    # Given
    client = ExecServiceClient(Manager())
    owned_http = client.http

    # When
    asyncio.run(client.aclose())

    # Then
    assert owned_http.is_closed


def test_lazily_created_gateway_closes_without_closing_injected_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from groken import gateway

    @final
    class OwnedManager:
        def __init__(self) -> None:
            self.closed = False

        def ensure_sandbox_metadata(self) -> object:
            return {
                "execDaemonUrl": "https://exec.test",
                "networkToken": "nt",
                "execDaemonAuthToken": "at",
                "podId": "p",
            }

        def close(self) -> None:
            self.closed = True

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=frame({}))

    manager = OwnedManager()
    external_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gateway, "GatewayManager", lambda: manager)
    client = ExecServiceClient(http_client=external_http)

    # When
    _ = asyncio.run(client.execute("true"))
    asyncio.run(client.aclose())

    # Then
    assert manager.closed
    assert not external_http.is_closed
    asyncio.run(external_http.aclose())


def test_exec_connect_request_and_split_response() -> None:
    manager = Manager()
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=frame(
                {"exec_client_message": {"shell_result": {"success": {"stdout": "ok"}}}}
            )
            + frame({}, 128),
        )

    client = ExecServiceClient(
        manager,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(client.execute("printf ok"))
    assert result == ExecResult("ok", "", 0)
    assert (
        str(seen[0].url)
        == "https://exec.test/agent.v1.ExecService/Exec?network_token=nt"
    )
    assert seen[0].headers["authorization"] == "Bearer at"
    assert seen[0].headers["content-type"] == "application/connect+json"
    body = decode_object(seen[0].content[5:])
    shell_args = require_object(body["shell_args"])
    assert shell_args["command"] == "printf ok"


def test_camel_case_live_response_and_stream_close() -> None:
    live_stdout = "Linux cursor 6.12.94+ #1 SMP PREEMPT_DYNAMIC Tue Jul 28 22:00:48 UTC 2026 x86_64 GNU/Linux\\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                frame(
                    {
                        "execClientMessage": {
                            "id": 1,
                            "shellResult": {
                                "success": {
                                    "command": "uname -a",
                                    "workingDirectory": "/workspace",
                                    "stdout": live_stdout,
                                }
                            },
                        }
                    }
                )
                + frame({"execClientControlMessage": {"streamClose": {"id": 1}}})
                + frame(
                    {
                        "execClientMessage": {
                            "shellResult": {
                                "failure": {"exit_code": 1, "stderr": "nope"}
                            }
                        }
                    }
                )
            ),
        )

    client = ExecServiceClient(
        Manager(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert asyncio.run(client.execute("uname -a")) == ExecResult(
        live_stdout,
        "nope",
        1,
    )


def test_malformed_or_failure_envelopes_fail_closed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                frame(
                    {
                        "exec_client_message": {
                            "shell_result": {"success": {"stdout": "bad"}}
                        },
                        "execClientMessage": {
                            "shellResult": {"success": {"stdout": "bad2"}}
                        },
                    }
                )
                + frame({})
            ),
        )

    client = PublicCallExecServiceClient(
        Manager(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert asyncio.run(client.call("https://exec.test", "nt", {}, b"")) == (
        ExecResult("", "", 0),
        False,
    )


def test_truncated_response_is_protocol_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=struct.pack(">BI", 0, 20) + b"{}")

    client = ExecServiceClient(
        Manager(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ExecProtocolError):
        _ = asyncio.run(client.execute("true"))
