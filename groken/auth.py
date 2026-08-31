import base64
import hashlib
import json
import secrets
import time
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import httpx
from typing_extensions import override

from .private_files import write_private_text

WEBSITE_URL = "https://cursor.com"
API_BASE_URL = "https://api2.cursor.sh"
PROD_AUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"

TOKEN_FILE = Path.home() / ".config" / "groken" / "tokens.json"

LocalStateReason = Literal["malformed JSON", "expected a JSON object"]


# Exception instances must remain mutable so Python can attach traceback state.
@dataclass(slots=True)
class TokenStateError(Exception):
    path: Path
    reason: LocalStateReason

    @override
    def __str__(self) -> str:
        return (
            f"token state is invalid at {self.path} ({self.reason}); "
            "remove it and run: groken login"
        )


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast("dict[str, object]", value)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def start_login(redirect_target: str = "sand") -> dict[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    uid = str(uuidlib.uuid4())
    login_url = (
        f"{WEBSITE_URL}/loginDeepControl?challenge={challenge}"
        f"&uuid={uid}&mode=login&redirectTarget={redirect_target}"
    )
    return {"uuid": uid, "verifier": verifier, "login_url": login_url}


def poll_for_tokens(uid: str, verifier: str, timeout_s: float = 300.0) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout_s
    with httpx.Client(timeout=15.0) as http:
        while time.monotonic() < deadline:
            try:
                r = http.get(
                    f"{API_BASE_URL}/auth/poll",
                    params={"uuid": uid, "verifier": verifier},
                    headers={"Content-Type": "application/json"},
                )
                if r.status_code == 200:
                    value = cast("object", r.json())
                    data = _object_dict(value)
                    if data is not None and "accessToken" in data and "refreshToken" in data:
                        save_tokens(data)
                        return data
            except httpx.HTTPError:
                pass
            time.sleep(1.5)
    return None


def refresh_tokens(refresh_token: str) -> dict[str, object] | None:
    r = httpx.post(
        f"{API_BASE_URL}/oauth/token",
        json={
            "client_id": PROD_AUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15.0,
    )
    if r.status_code != 200:
        return None
    value = cast("object", r.json())
    data = _object_dict(value)
    if data is None:
        raise TypeError("token response must be a JSON object")
    merged = load_tokens() or {}
    merged.update({key: item for key, item in data.items() if item})
    if "access_token" in merged:
        merged["accessToken"] = str(merged.pop("access_token"))
    if "refresh_token" in merged:
        merged["refreshToken"] = str(merged.pop("refresh_token"))
    save_tokens(merged)
    return merged


def save_tokens(tokens: dict[str, object]) -> None:
    write_private_text(TOKEN_FILE, json.dumps(tokens, indent=2))


def load_tokens() -> dict[str, object] | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        value = cast("object", json.loads(TOKEN_FILE.read_text()))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise TokenStateError(TOKEN_FILE, "malformed JSON") from exc
    tokens = _object_dict(value)
    if tokens is None:
        raise TokenStateError(TOKEN_FILE, "expected a JSON object")
    return tokens


def get_access_token() -> str:
    tokens = load_tokens()
    if not tokens or "accessToken" not in tokens:
        raise SystemExit("No tokens. Run: groken login")
    return str(tokens["accessToken"])
