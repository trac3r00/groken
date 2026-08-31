from __future__ import annotations

import base64
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, final
from urllib.parse import urlparse

from typing_extensions import override

from .vnc_proxy import VncProxySession

_DEFAULT_TIMEOUT: Final = 180.0
_DEFAULT_INTERVAL: Final = 0.5
_PROBE_CAP: Final = 2.0


@final
@dataclass(frozen=True, slots=True)
class VncNotReadyError(Exception):
    reason: str

    @override
    def __str__(self) -> str:
        return f"vnc not ready: {self.reason}"


def wait_until_vnc_ready(
    session: VncProxySession,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    interval: float = _DEFAULT_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = clock() + timeout
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise VncNotReadyError("timed out waiting for desktop")
        if _rfb_ready(session, min(_PROBE_CAP, remaining)):
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise VncNotReadyError("timed out waiting for desktop")
        sleep(min(interval, remaining))


def _rfb_ready(session: VncProxySession, timeout: float) -> bool:
    try:
        return _read_rfb_banner(session.origin, session.websocket_path, timeout)
    except (OSError, TimeoutError):
        return False


def _read_rfb_banner(origin: str, websocket_path: str, timeout: float) -> bool:
    parsed = urlparse(origin)
    host = parsed.hostname
    if host is None:
        return False
    port = parsed.port or 80
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        request = "".join(
            (
                f"GET {websocket_path} HTTP/1.1\r\n",
                f"Host: {host}:{port}\r\n",
                "Upgrade: websocket\r\n",
                "Connection: Upgrade\r\n",
                "Sec-WebSocket-Version: 13\r\n",
                f"Sec-WebSocket-Key: {key}\r\n",
                "\r\n",
            )
        )
        sock.sendall(request.encode())
        header, rest = _recv_headers(sock)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            return False
        buf = rest
        while b"RFB " not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            buf += chunk
        return True


def _recv_headers(sock: socket.socket) -> tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("closed before headers")
        buf += chunk
    header, rest = buf.split(b"\r\n\r\n", 1)
    return header, rest
