import base64
import hashlib
import json
import secrets
import time
import uuid as uuidlib
from pathlib import Path

import httpx

WEBSITE_URL = "https://cursor.com"
API_BASE_URL = "https://api2.cursor.sh"
PROD_AUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"

TOKEN_FILE = Path.home() / ".config" / "groken" / "tokens.json"


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
                    data = r.json()
                    if "accessToken" in data and "refreshToken" in data:
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
    data = r.json()
    merged = load_tokens() or {}
    merged.update({k: v for k, v in data.items() if v})
    if "access_token" in merged:
        merged["accessToken"] = str(merged.pop("access_token"))
    if "refresh_token" in merged:
        merged["refreshToken"] = str(merged.pop("refresh_token"))
    save_tokens(merged)
    return merged


def save_tokens(tokens: dict[str, object]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(0o600)


def load_tokens() -> dict[str, object] | None:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def get_access_token() -> str:
    tokens = load_tokens()
    if not tokens or "accessToken" not in tokens:
        raise SystemExit("No tokens. Run: groken login")
    return str(tokens["accessToken"])
