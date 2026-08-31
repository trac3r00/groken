import os
import plistlib
import sys
import uuid
from pathlib import Path
from typing import cast, final
from xml.parsers.expat import ExpatError

import httpx

from .auth import get_access_token, load_tokens, refresh_tokens
from .checksum import create_cursor_checksum, get_machine_id
from .config import load_config, save_config

BACKEND_URL = os.environ.get("SAND_BACKEND_URL", "https://api2.cursor.sh").rstrip("/")
CLIENT_TYPE = "sand"
APP_PATH = Path("/Applications/Grok Bot.app/Contents/Info.plist")


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast("dict[str, object]", value)


def detect_client_version() -> str:
    override = os.environ.get("SAND_CLIENT_VERSION")
    if override:
        return override
    try:
        plist_value = cast("object", plistlib.loads(APP_PATH.read_bytes()))
        plist = _object_dict(plist_value)
        if plist is None:
            raise ValueError("application plist must contain a dictionary")
        version = plist.get("CFBundleShortVersionString")
        if isinstance(version, str) and version:
            cfg = load_config()
            cfg["client_version"] = version
            save_config(cfg)
            return version
    except (OSError, ValueError, ExpatError):
        pass

    try:
        cached = load_config().get("client_version")
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, str) and cached:
        print("Cursor app not found; using cached client version", file=sys.stderr)
        return cached
    return "0.20.0"

GROK_BOT = "aiserver.v1.GrokBotService"
DASHBOARD = "aiserver.v1.DashboardService"


@final
class ConnectError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"connect error {status}: {body[:400]}")
        self.status = status
        self.body = body


@final
class SandClient:
    def __init__(self, access_token: str | None = None):
        self.access_token = access_token or get_access_token()
        self.machine_id = get_machine_id()
        self.http = httpx.Client(timeout=httpx.Timeout(30.0, read=300.0))
        self.client_version = detect_client_version()

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.access_token}",
            "x-cursor-checksum": create_cursor_checksum(self.machine_id),
            "x-cursor-client-type": CLIENT_TYPE,
            "x-cursor-client-version": self.client_version,
            "x-sand-box-namespace": "prod",
            "x-ghost-mode": "false",
            "x-request-id": str(uuid.uuid4()),
            "connect-protocol-version": "1",
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _relogin_once(self) -> bool:
        tokens = load_tokens()
        if not tokens or "refreshToken" not in tokens:
            return False
        fresh = refresh_tokens(str(tokens["refreshToken"]))
        if fresh and "accessToken" in fresh:
            self.access_token = str(fresh["accessToken"])
            return True
        return False

    def unary(self, service: str, method: str, payload: dict[str, object]) -> dict[str, object]:
        url = f"{BACKEND_URL}/{service}/{method}"
        for attempt in range(2):
            r = self.http.post(url, headers=self._headers(), json=payload)
            if r.status_code == 401 and attempt == 0 and self._relogin_once():
                continue
            if r.status_code != 200:
                raise ConnectError(r.status_code, r.text)
            value = cast("object", r.json())
            response = _object_dict(value)
            if response is None:
                raise TypeError("RPC response must be a JSON object")
            return response
        raise ConnectError(401, "unauthorized after refresh")

    def list_sandboxes(self) -> dict[str, object]:
        return self.unary(GROK_BOT, "ListSandBoxes", {})

    def list_sand_mcp_tools(self, server_identifiers: list[str] | None = None) -> dict[str, object]:
        return self.unary(
            DASHBOARD,
            "ListSandMcpTools",
            {"serverIdentifiers": list(server_identifiers or [])},
        )

    def execute_sand_mcp_tool(
        self,
        *,
        server_identifier: str,
        tool_name: str,
        arguments: dict[str, object],
        tool_call_id: str,
        agent_id: str,
    ) -> dict[str, object]:
        return self.unary(
            DASHBOARD,
            "ExecuteSandMcpTool",
            {
                "serverIdentifier": server_identifier,
                "toolName": tool_name,
                "args": arguments,
                "toolCallId": tool_call_id,
                "agentId": agent_id,
            },
        )
