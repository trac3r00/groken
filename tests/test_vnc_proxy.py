from __future__ import annotations

import gzip
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from typing_extensions import override

from groken.vnc_proxy import PORT_TOKEN_HEADER, serve_vnc_proxy

TOKEN = "test-port-token"
OTHER_TOKEN = "other-port-token"


class _Origin(BaseHTTPRequestHandler):
    expected_token: ClassVar[str] = TOKEN
    label: ClassVar[str] = "first"
    paths: ClassVar[list[str]] = []

    @override
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.paths.append(self.path)
        if self.headers.get(PORT_TOKEN_HEADER) != self.expected_token:
            self.send_error(404, "missing token")
            return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            _ = self.wfile.write(f"upgraded {self.path}".encode())
            return
        body = f"{self.label} {self.path}".encode()
        encoded_body = gzip.compress(body) if self.path == "/compressed.css" else body
        self.send_response(200)
        self.send_header("Content-Type", "text/css")
        if encoded_body is not body:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(encoded_body)))
        self.end_headers()
        _ = self.wfile.write(encoded_body)


class _OtherOrigin(_Origin):
    expected_token = OTHER_TOKEN
    label = "second"
    paths: ClassVar[list[str]] = []


def _serve(handler: type[_Origin]) -> tuple[ThreadingHTTPServer, str]:
    handler.paths = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    bound_address = httpd.server_address
    assert len(bound_address) == 2
    host, port = bound_address
    return (
        httpd,
        f"http://{host}:{port}/vnc.html?port_token={handler.expected_token}",
    )


def test_origin_404s_assets_without_token() -> None:
    httpd, url = _serve(_Origin)
    try:
        origin = url.rsplit("/vnc.html", 1)[0]
        response = httpx.get(f"{origin}/app/styles/base.css", timeout=2.0)
        assert response.status_code == 404
    finally:
        httpd.shutdown()


def test_proxy_requires_capability_and_preserves_relative_assets() -> None:
    # Given
    httpd, upstream = _serve(_Origin)
    session = serve_vnc_proxy(upstream)
    parsed_viewer = urlparse(session.local_url)
    capability = parsed_viewer.path.split("/")[1]
    asset_url = f"{session.origin}/{capability}/app/styles/base.css"

    try:
        # When
        response = httpx.get(asset_url, timeout=2.0)
        unscoped = httpx.get(f"{session.origin}/app/styles/base.css", timeout=2.0)

        # Then
        assert response.status_code == 200
        assert response.text == "first /app/styles/base.css"
        assert unscoped.status_code == 404
        assert len(capability) >= 32
        assert session.websocket_path == f"/{capability}"
        assert parse_qs(parsed_viewer.query)["path"] == [capability]
        assert TOKEN not in session.local_url
        assert TOKEN not in session.websocket_path
        assert "port_token=" not in session.local_url
        assert "port_token=" not in session.websocket_path
    finally:
        session.close()
        httpd.shutdown()


def test_proxy_drops_content_encoding_after_httpx_decodes_body() -> None:
    # Given
    httpd, upstream = _serve(_Origin)
    session = serve_vnc_proxy(upstream)
    capability = urlparse(session.local_url).path.split("/")[1]

    try:
        # When
        response = httpx.get(
            f"{session.origin}/{capability}/compressed.css", timeout=2.0
        )

        # Then
        assert response.content == b"first /compressed.css"
        assert "content-encoding" not in response.headers
    finally:
        session.close()
        httpd.shutdown()


def test_proxy_upgrades_websocket_through_local_capability() -> None:
    # Given
    httpd, upstream = _serve(_Origin)
    session = serve_vnc_proxy(f"{upstream}&path=websockify%3Ftoken%3D2")
    request_path = session.websocket_path.encode()
    try:
        parsed_host, parsed_port = session.origin.removeprefix("http://").split(":")
        sock = socket.create_connection((parsed_host, int(parsed_port)), timeout=2)
        try:
            # When
            sock.sendall(
                b"".join(
                    (
                        b"GET " + request_path + b" HTTP/1.1\r\n",
                        b"Host: 127.0.0.1\r\n",
                        b"Upgrade: websocket\r\n",
                        b"Connection: Upgrade\r\n",
                        b"Sec-WebSocket-Version: 13\r\n",
                        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n",
                        b"\r\n",
                    )
                )
            )
            reply = b""
            while b"upgraded" not in reply:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                reply += chunk
        finally:
            sock.close()

        # Then
        assert b" 101 " in reply.split(b"\r\n", 1)[0]
        assert b"upgraded /websockify?token=2" in reply
        assert "token=2" not in session.local_url
        assert "token=2" not in session.websocket_path
    finally:
        session.close()
        httpd.shutdown()


def test_proxy_rejects_absolute_target_before_upstream_call() -> None:
    # Given
    httpd, upstream = _serve(_Origin)
    session = serve_vnc_proxy(upstream)
    absolute_target = upstream.split("/vnc.html", 1)[0] + "/captured"
    try:
        parsed_host, parsed_port = session.origin.removeprefix("http://").split(":")
        with socket.create_connection(
            (parsed_host, int(parsed_port)), timeout=2
        ) as sock:
            # When
            sock.sendall(
                f"GET {absolute_target} HTTP/1.1\r\nHost: local\r\n\r\n".encode()
            )
            reply = sock.recv(1024)

        # Then
        assert b" 400 " in reply.split(b"\r\n", 1)[0]
        assert _Origin.paths == []
    finally:
        session.close()
        httpd.shutdown()


def test_proxy_config_is_isolated_when_sessions_overlap() -> None:
    # Given
    first_httpd, first_upstream = _serve(_Origin)
    second_httpd, second_upstream = _serve(_OtherOrigin)
    first_session = serve_vnc_proxy(first_upstream)
    second_session = serve_vnc_proxy(second_upstream)
    first_capability = urlparse(first_session.local_url).path.split("/")[1]
    second_capability = urlparse(second_session.local_url).path.split("/")[1]

    try:
        # When
        first = httpx.get(
            f"{first_session.origin}/{first_capability}/asset.js", timeout=2.0
        )
        second = httpx.get(
            f"{second_session.origin}/{second_capability}/asset.js", timeout=2.0
        )

        # Then
        assert first.text == "first /asset.js"
        assert second.text == "second /asset.js"
        assert first_capability != second_capability
    finally:
        first_session.close()
        second_session.close()
        first_httpd.shutdown()
        second_httpd.shutdown()


def test_missing_port_token_is_clear() -> None:
    with pytest.raises(ValueError, match="missing port_token"):
        _ = serve_vnc_proxy("https://example.invalid/vnc.html")
