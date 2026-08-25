"""noVNC URL minting."""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, quote, urlparse


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def mint_jwt(
    network_token: str,
    tenant_id: str,
    pod_id: str,
    now: float | None = None,
    *,
    container_port: int = 6080,
) -> str:
    current = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"aud": tenant_id, "exp": current + 600, "nbf": current - 10,
              "pod_id": pod_id, "container_port": container_port, "iat": current}
    encoded = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = hmac.new(network_token.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64(signature)}"


def display_from_forever_box(status: dict[str, object]) -> int | None:
    raw = status.get("vncUrl")
    if not isinstance(raw, str) or not raw:
        return None
    viewer_path = parse_qs(urlparse(raw).query).get("path", [""])[0]
    token = parse_qs(urlparse(viewer_path).query).get("token", [""])[0]
    try:
        display = int(token)
    except ValueError:
        return None
    return display if display > 0 else None


def vnc_url(
    metadata: dict[str, object],
    now: float | None = None,
    *,
    display: int = 1,
) -> str:
    if display < 1:
        raise ValueError("display must be at least 1")
    key = "vncUrl" if display == 1 else "forkVncBaseUrl"
    raw = metadata.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"sandbox metadata is missing {key}")
    parsed = urlparse(raw)
    host = parsed.hostname
    if not host:
        raise ValueError(f"sandbox metadata {key} has no host")
    parts = host.split(".", 1)[0].split("-")
    if len(parts) < 3:
        raise ValueError(f"sandbox metadata {key} has invalid host")
    tenant_id = parts[0]
    token = metadata.get("networkToken")
    pod_id = metadata.get("podId")
    if not isinstance(token, str) or not isinstance(pod_id, str):
        raise TypeError("sandbox metadata is missing networkToken or podId")
    container_port = 6080 if display == 1 else 6081
    jwt = mint_jwt(token, tenant_id, pod_id, now, container_port=container_port)
    viewer_path = "" if display == 1 else f"&path={quote(f'websockify?token={display}', safe='')}"
    return (
        f"{parsed.scheme or 'https'}://{host}/vnc.html?port_token={jwt}"
        f"&autoconnect=1&resize=scale{viewer_path}"
    )
