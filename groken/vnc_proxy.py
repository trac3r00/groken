from __future__ import annotations

import secrets
import select
import socket
import ssl
import threading
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, cast, final
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import httpx
from typing_extensions import override

PORT_TOKEN_HEADER: Final = "x-anyrun-port-token"
_HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True, slots=True)
class _ProxyConfig:
    origin: str
    token: str
    websocket_target: str
    capability_path: str
    client: httpx.Client


@dataclass
class VncProxySession:
    origin: str
    local_url: str
    websocket_path: str
    _httpd: ThreadingHTTPServer
    _client: httpx.Client
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._httpd.shutdown()
        self._httpd.server_close()
        self._client.close()


def _token_from_url(url: str) -> str:
    parsed = urlparse(url)
    token = parse_qs(parsed.query).get("port_token", [""])[0]
    if not token:
        raise ValueError("vnc url is missing port_token")
    return token


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname is None:
        raise ValueError("vnc url is missing host")
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _viewer_query(url: str, capability_path: str) -> str:
    pairs = [
        (key, value)
        for key, value in parse_qsl(urlparse(url).query)
        if key not in {"path", "port_token"}
    ]
    pairs.append(("path", capability_path.lstrip("/")))
    return urlencode(pairs)


def _websocket_path(url: str) -> str:
    path = parse_qs(urlparse(url).query).get("path", ["websockify"])[0]
    return f"/{path.lstrip('/')}"


@final
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: ThreadingHTTPServer,
        *,
        config: _ProxyConfig,
    ) -> None:
        self._config = config
        super().__init__(request, client_address, server)

    @override
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        is_websocket = self.headers.get("Upgrade", "").lower() == "websocket"
        target = self._upstream_target(websocket=is_websocket)
        if target is None:
            return
        if is_websocket:
            self._tunnel_websocket(target)
            return
        self._proxy_http(target)

    def do_HEAD(self) -> None:
        target = self._upstream_target(websocket=False)
        if target is not None:
            self._proxy_http(target)

    def _upstream_target(self, *, websocket: bool) -> str | None:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self.send_error(400, "absolute proxy targets are forbidden")
            return None
        if parsed.path == self._config.capability_path:
            if websocket:
                return self._config.websocket_target
            self.send_error(404)
            return None
        prefix = f"{self._config.capability_path}/"
        if not parsed.path.startswith(prefix):
            self.send_error(404)
            return None
        upstream_path = parsed.path.removeprefix(self._config.capability_path)
        return urlunsplit(("", "", upstream_path, parsed.query, ""))

    def _proxy_http(self, target: str) -> None:
        response = self._config.client.request(self.command, target)
        body = response.content
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            if key.lower() in {"content-encoding", "content-length"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            _ = self.wfile.write(body)

    def _tunnel_websocket(self, target: str) -> None:
        origin = urlparse(self._config.origin)
        host = origin.hostname
        if host is None:
            self.send_error(502, "origin has no host")
            return
        port = origin.port or (443 if origin.scheme == "https" else 80)
        raw = socket.create_connection((host, port), timeout=30)
        sock: socket.socket
        if origin.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
        try:
            sock.sendall(self._origin_upgrade_request(origin.netloc, target))
            self._shuttle(sock)
        finally:
            sock.close()

    def _origin_upgrade_request(self, netloc: str, target: str) -> bytes:
        lines = [f"{self.command} {target} {self.request_version}"]
        for key, value in self.headers.items():
            if key.lower() == "host":
                continue
            lines.append(f"{key}: {value}")
        lines.append(f"Host: {netloc}")
        lines.append(f"{PORT_TOKEN_HEADER}: {self._config.token}")
        lines.append("")
        lines.append("")
        return "\r\n".join(lines).encode("latin-1")

    def _shuttle(self, origin: socket.socket) -> None:
        client = cast("socket.socket", self.connection)
        sockets = [client, origin]
        while True:
            readable, _, broken = select.select(sockets, [], sockets, 60)
            if broken or not readable:
                return
            for source in readable:
                payload = source.recv(65536)
                if not payload:
                    return
                dest = origin if source is client else client
                dest.sendall(payload)


def serve_vnc_proxy(
    upstream_url: str, *, host: str = "127.0.0.1", port: int = 0
) -> VncProxySession:
    token = _token_from_url(upstream_url)
    origin = _origin_from_url(upstream_url)
    capability_path = f"/{secrets.token_urlsafe(32)}"
    client = httpx.Client(
        base_url=origin,
        headers={PORT_TOKEN_HEADER: token},
        timeout=30.0,
        follow_redirects=False,
    )
    config = _ProxyConfig(
        origin=origin,
        token=token,
        websocket_target=_websocket_path(upstream_url),
        capability_path=capability_path,
        client=client,
    )
    httpd = ThreadingHTTPServer((host, port), partial(_Handler, config=config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    bound_address = httpd.server_address
    bound_host = str(bound_address[0])
    bound_port = int(bound_address[1])
    local_origin = f"http://{bound_host}:{bound_port}"
    viewer_query = _viewer_query(upstream_url, capability_path)
    return VncProxySession(
        origin=local_origin,
        local_url=f"{local_origin}{capability_path}/vnc.html?{viewer_query}",
        websocket_path=capability_path,
        _httpd=httpd,
        _client=client,
    )
