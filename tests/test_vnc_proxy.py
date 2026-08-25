from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from groken.vnc_proxy import PORT_TOKEN_HEADER, serve_vnc_proxy

TOKEN = "test-port-token"


class _Origin(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.headers.get(PORT_TOKEN_HEADER) != TOKEN:
            self.send_error(404, "missing token")
            return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.write(b"upgraded")
            return
        body = f"ok {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/css")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}/vnc.html?port_token={TOKEN}"


def test_origin_404s_assets_without_token() -> None:
    httpd, url = _serve(_Origin)
    try:
        origin = url.rsplit("/vnc.html", 1)[0]
        response = httpx.get(f"{origin}/app/styles/base.css", timeout=2.0)
        assert response.status_code == 404
    finally:
        httpd.shutdown()


def test_proxy_serves_assets_without_query_token() -> None:
    httpd, upstream = _serve(_Origin)
    session = serve_vnc_proxy(upstream)
    try:
        response = httpx.get(f"{session.origin}/app/styles/base.css", timeout=2.0)
        assert response.status_code == 200
        assert response.text == "ok /app/styles/base.css"
        assert "port_token=" not in session.local_url
    finally:
        session.close()
        httpd.shutdown()


def test_proxy_upgrades_websocket_without_client_token() -> None:
    httpd, upstream = _serve(_Origin)
    session = serve_vnc_proxy(upstream)
    try:
        parsed_host, parsed_port = session.origin.removeprefix("http://").split(":")
        sock = socket.create_connection((parsed_host, int(parsed_port)), timeout=2)
        try:
            sock.sendall(
                b"GET /websockify HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Version: 13\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"\r\n"
            )
            reply = b""
            while b"upgraded" not in reply:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                reply += chunk
        finally:
            sock.close()
        assert b" 101 " in reply.split(b"\r\n", 1)[0]
        assert b"upgraded" in reply
    finally:
        session.close()
        httpd.shutdown()


def test_missing_port_token_is_clear() -> None:
    with pytest.raises(ValueError, match="missing port_token"):
        serve_vnc_proxy("https://example.invalid/vnc.html")
