"""noVNC URL minting."""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlparse


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def mint_jwt(network_token: str, tenant_id: str, pod_id: str, now: int | float | None = None) -> str:
    current = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"aud": tenant_id, "exp": current + 600, "nbf": current - 10,
              "pod_id": pod_id, "container_port": 6080, "iat": current}
    encoded = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = hmac.new(network_token.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64(signature)}"


def vnc_url(metadata: dict[str, object], now: int | float | None = None) -> str:
    raw = metadata.get("vncUrl")
    if not isinstance(raw, str) or not raw:
        raise ValueError("sandbox metadata is missing vncUrl")
    parsed = urlparse(raw)
    host = parsed.hostname
    if not host:
        raise ValueError("sandbox metadata vncUrl has no host")
    parts = host.split(".", 1)[0].split("-")
    if len(parts) < 3:
        raise ValueError("sandbox metadata vncUrl has invalid host")
    tenant_id = parts[0]
    token = metadata.get("networkToken")
    pod_id = metadata.get("podId")
    if not isinstance(token, str) or not isinstance(pod_id, str):
        raise ValueError("sandbox metadata is missing networkToken or podId")
    jwt = mint_jwt(token, tenant_id, pod_id, now)
    return f"{parsed.scheme or 'https'}://{host}/vnc.html?port_token={jwt}"
