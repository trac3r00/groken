from __future__ import annotations

import json
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .parsing import build_parsing_result


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str


class ExecServiceError(Exception):
    pass


class ExecProtocolError(ExecServiceError):
    pass


class ExecRemoteError(ExecServiceError):
    pass


class ExecIndeterminateError(ExecServiceError):
    pass


class ExecServiceClient:
    def __init__(self, manager=None, *, http_client=None, metadata_ttl_s=60, clock=time.monotonic):
        self.manager = manager
        self.http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=30))
        self.metadata_ttl_s = metadata_ttl_s
        self.clock = clock
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0

    async def _metadata_for_call(self, force: bool = False) -> dict[str, Any]:
        if not force and self._metadata is not None and self.clock() - self._metadata_at < self.metadata_ttl_s:
            return self._metadata
        if self.manager is None:
            from .gateway import GatewayManager
            self.manager = GatewayManager()
        try:
            value = self.manager.ensure_sandbox_metadata()
        except Exception as exc:
            raise ExecServiceError("sandbox metadata unavailable") from exc
        if not isinstance(value, dict):
            raise ExecServiceError("sandbox metadata unavailable")
        required = ("execDaemonUrl", "networkToken", "execDaemonAuthToken", "podId")
        if any(not value.get(k) for k in required):
            raise ExecServiceError("sandbox metadata unavailable")
        self._metadata, self._metadata_at = value, self.clock()
        return value

    @staticmethod
    def _envelope(payload: bytes) -> bytes:
        return struct.pack(">BI", 0, len(payload)) + payload

    async def execute(self, command: str, working_directory: str = "/workspace", timeout_ms: int = 15000) -> ExecResult:
        if not command or not command.strip():
            raise ValueError("command must not be empty")
        exec_id = str(uuid.uuid4())
        reminted = False
        while True:
            metadata = await self._metadata_for_call(reminted)
            body = {"id": 1, "exec_id": exec_id, "shell_args": {
                "command": command, "working_directory": working_directory, "timeout": timeout_ms,
                "tool_call_id": "groken", "skip_approval": True, "simple_commands": [command],
                "has_input_redirect": False, "has_output_redirect": False,
                "parsing_result": build_parsing_result(command),
            }}
            url = str(metadata["execDaemonUrl"]).rstrip("/") + "/agent.v1.ExecService/Exec"
            headers = {"content-type": "application/connect+json", "connect-protocol-version": "1",
                       "authorization": "Bearer " + str(metadata["execDaemonAuthToken"])}
            try:
                result, unauth = await self._call(url, str(metadata["networkToken"]), headers, self._envelope(json.dumps(body, separators=(",", ":")).encode()))
            except httpx.HTTPStatusError as exc:
                unauth = exc.response.status_code == 401
                if not unauth:
                    raise ExecServiceError("exec request failed") from exc
                result = None
            if unauth and not reminted:
                self._metadata = None
                reminted = True
                continue
            if unauth:
                raise ExecRemoteError("exec request unauthorized")
            return result

    async def _call(self, url: str, token: str, headers: dict[str, str], body: bytes):
        seen = False
        stdout, stderr = "", ""
        async with self.http.stream("POST", url, params={"network_token": token}, headers=headers, content=body) as response:
            if response.status_code == 401:
                return None, True
            if response.status_code >= 400:
                raise ExecServiceError("exec request failed")
            buf = b""
            async for chunk in response.aiter_bytes():
                buf += chunk
                if len(buf) > 12 * 1024 * 1024:
                    raise ExecProtocolError("response too large")
                while len(buf) >= 5:
                    flags, length = struct.unpack(">BI", buf[:5])
                    if length > 4 * 1024 * 1024:
                        raise ExecProtocolError("frame too large")
                    if len(buf) < 5 + length:
                        break
                    payload, buf = buf[5:5 + length], buf[5 + length:]
                    try: data = json.loads(payload)
                    except (ValueError, UnicodeDecodeError) as exc: raise ExecProtocolError("invalid response frame") from exc
                    if flags & 0x80:
                        error = data.get("error") if isinstance(data, dict) else None
                        if error and str(error.get("code", error.get("message", ""))).lower() in {"unauthenticated", "401"}:
                            if seen: raise ExecIndeterminateError("execution state is indeterminate")
                            return None, True
                        if error: raise ExecRemoteError("remote execution failed")
                    else:
                        seen = True
                        msg = data.get("exec_client_message", {}).get("shell_result", {})
                        if "success" in msg: stdout += str(msg["success"].get("stdout", ""))
                        elif "failure" in msg: stderr += str(msg["failure"].get("stderr", "")); return ExecResult(stdout, stderr), False
                        if len(stdout) + len(stderr) > 10 * 1024 * 1024: raise ExecProtocolError("output too large")
            if buf: raise ExecProtocolError("truncated response frame")
        return ExecResult(stdout, stderr), False
