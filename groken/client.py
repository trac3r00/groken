import os
import plistlib
import uuid
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

import httpx

from .auth import get_access_token, load_tokens, refresh_tokens
from .checksum import create_cursor_checksum, get_machine_id

BACKEND_URL = os.environ.get("SAND_BACKEND_URL", "https://api2.cursor.sh").rstrip("/")
CLIENT_TYPE = "sand"
APP_PATH = Path("/Applications/Grok Bot.app/Contents/Info.plist")


def detect_client_version() -> str:
    override = os.environ.get("SAND_CLIENT_VERSION")
    if override:
        return override
    try:
        plist = plistlib.loads(APP_PATH.read_bytes())
        version = plist.get("CFBundleShortVersionString")
        if isinstance(version, str) and version:
            return version
    except (OSError, ValueError, ExpatError):
        pass
    return "0.20.0"

GROK_BOT = "aiserver.v1.GrokBotService"


class ConnectError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"connect error {status}: {body[:400]}")
        self.status = status
        self.body = body


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

    def unary(self, service: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{BACKEND_URL}/{service}/{method}"
        for attempt in range(2):
            r = self.http.post(url, headers=self._headers(), json=payload)
            if r.status_code == 401 and attempt == 0 and self._relogin_once():
                continue
            if r.status_code != 200:
                raise ConnectError(r.status_code, r.text)
            return r.json()
        raise ConnectError(401, "unauthorized after refresh")

    def list_sandboxes(self) -> dict[str, Any]:
        return self.unary(GROK_BOT, "ListSandBoxes", {})
