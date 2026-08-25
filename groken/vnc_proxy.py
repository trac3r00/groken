from __future__ import annotations

import select
import socket
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, Final
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import httpx

PORT_TOKEN_HEADER: Final = "x-anyrun-port-token"
_HOP_BY_HOP: Final = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})


@dataclass(frozen=True)
class _ProxyConfig:
    origin: str
    token: str
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


def _viewer_query(url: str) -> str:
    pairs = ((key, value) for key, value in parse_qsl(urlparse(url).query) if key != "port_token")
    return urlencode(tuple(pairs))


def _websocket_path(url: str) -> str:
    path = parse_qs(urlparse(url).query).get("path", ["websockify"])[0]
    return f"/{path.lstrip('/')}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config: ClassVar[_ProxyConfig]

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._tunnel_websocket()
            return
        self._proxy_http()

    def do_HEAD(self) -> None:
        self._proxy_http()

    def _proxy_http(self) -> None:
        response = self.config.client.request(self.command, self.path)
        body = response.content
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            if key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _tunnel_websocket(self) -> None:
        origin = urlparse(self.config.origin)
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
            sock.sendall(self._origin_upgrade_request(origin.netloc))
            self._shuttle(sock)
        finally:
            sock.close()

    def _origin_upgrade_request(self, netloc: str) -> bytes:
        lines = [f"{self.command} {self.path} {self.request_version}"]
        for key, value in self.headers.items():
            if key.lower() == "host":
                continue
            lines.append(f"{key}: {value}")
        lines.append(f"Host: {netloc}")
        lines.append(f"{PORT_TOKEN_HEADER}: {self.config.token}")
        lines.append("")
        lines.append("")
        return "\r\n".join(lines).encode("latin-1")

    def _shuttle(self, origin: socket.socket) -> None:
        client = self.connection
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


def serve_vnc_proxy(upstream_url: str, *, host: str = "127.0.0.1", port: int = 0) -> VncProxySession:
    token = _token_from_url(upstream_url)
    origin = _origin_from_url(upstream_url)
    client = httpx.Client(
        base_url=origin,
        headers={PORT_TOKEN_HEADER: token},
        timeout=30.0,
        follow_redirects=False,
    )
    _Handler.config = _ProxyConfig(origin=origin, token=token, client=client)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    bound_address = httpd.server_address
    bound_host = str(bound_address[0])
    bound_port = int(bound_address[1])
    local_origin = f"http://{bound_host}:{bound_port}"
    viewer_query = _viewer_query(upstream_url)
    query_suffix = f"?{viewer_query}" if viewer_query else ""
    return VncProxySession(
        origin=local_origin,
        local_url=f"{local_origin}/vnc.html{query_suffix}",
        websocket_path=_websocket_path(upstream_url),
        _httpd=httpd,
        _client=client,
    )
