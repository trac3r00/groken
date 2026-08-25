from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from groken import cli
from groken.vnc_proxy import PORT_TOKEN_HEADER, serve_vnc_proxy
from groken.vnc_ready import VncNotReadyError, wait_until_vnc_ready

TOKEN = "test-port-token"
RFB_BANNER = b"RFB 003.008\n"


class _DelayedOrigin(BaseHTTPRequestHandler):
    ready_after: int = 3
    hits: int = 0

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.headers.get(PORT_TOKEN_HEADER) != TOKEN:
            self.send_error(404, "missing token")
            return
        if "websockify" in self.path:
            type(self).hits += 1
            if type(self).hits < self.ready_after:
                self.send_error(503, "not ready")
                return
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.write(bytes([0x82, len(RFB_BANNER)]) + RFB_BANNER)
            return
        body = b"<html>vnc</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    handler.hits = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}/vnc.html?port_token={TOKEN}"


def test_wait_ready_returns_only_after_rfb_banner() -> None:
    httpd, upstream = _serve(_DelayedOrigin)
    session = serve_vnc_proxy(upstream)
    try:
        wait_until_vnc_ready(session, timeout=2.0, interval=0.0, sleep=lambda _: None)
        assert _DelayedOrigin.hits >= 3
    finally:
        session.close()
        httpd.shutdown()


def test_wait_ready_uses_fork_websocket_token() -> None:
    class _ForkOrigin(_DelayedOrigin):
        ready_after = 1

        def do_GET(self) -> None:
            if self.path != "/websockify?token=2":
                self.send_error(404, "wrong fork")
                return
            super().do_GET()

    httpd, upstream = _serve(_ForkOrigin)
    session = serve_vnc_proxy(f"{upstream}&path=websockify%3Ftoken%3D2")
    try:
        wait_until_vnc_ready(session, timeout=2.0, interval=0.0, sleep=lambda _: None)
        assert "path=websockify%3Ftoken%3D2" in session.local_url
        assert "port_token=" not in session.local_url
    finally:
        session.close()
        httpd.shutdown()


def test_wait_ready_times_out_without_rfb() -> None:
    class _NeverReady(_DelayedOrigin):
        ready_after = 10_000

    httpd, upstream = _serve(_NeverReady)
    session = serve_vnc_proxy(upstream)
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(dt: float) -> None:
        now[0] += dt

    try:
        with pytest.raises(VncNotReadyError, match="timed out"):
            wait_until_vnc_ready(session, timeout=0.2, interval=0.05, clock=clock, sleep=sleep)
    finally:
        session.close()
        httpd.shutdown()


def test_cmd_vnc_opens_configured_bot_display_after_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    selected: list[int] = []
    commands: list[tuple[str, object]] = []

    class _Manager:
        def own_agent_id(self) -> str:
            return "groken-id"

        def command(self, method: str, args: object) -> dict[str, object]:
            commands.append((method, args))
            return {
                "agentId": "groken-id",
                "state": "running",
                "vncUrl": "http://127.0.0.1:6081/vnc.html?path=websockify%3Ftoken%3D2",
            }

        def ensure_sandbox_metadata(self) -> dict[str, str]:
            return {
                "vncUrl": "https://tenant-pod-6080.example/vnc.html",
                "forkVncBaseUrl": "https://tenant-pod-6081.example",
                "networkToken": "secret",
                "podId": "pod",
            }

    class _Session:
        origin = "http://127.0.0.1:9"
        local_url = "http://127.0.0.1:9/vnc.html"

        def close(self) -> None:
            return

    class _Interrupt:
        def wait(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_manager", lambda: _Manager())
    monkeypatch.setattr(
        "groken.vnc.vnc_url",
        lambda metadata, now=None, *, display=1: selected.append(display)
        or "https://x/vnc.html?port_token=t",
    )
    monkeypatch.setattr("groken.vnc_proxy.serve_vnc_proxy", lambda url: _Session())
    monkeypatch.setattr(
        "groken.vnc_ready.wait_until_vnc_ready",
        lambda session, **kwargs: order.append("wait"),
    )
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: order.append(f"open:{url}"))
    monkeypatch.setattr(cli.threading, "Event", _Interrupt)

    cli.cmd_vnc(False)

    assert commands == [("getForeverBoxStatus", {"id": "groken-id"})]
    assert selected == [2]
    assert order == ["wait", "open:http://127.0.0.1:9/vnc.html"]


def test_cmd_vnc_ensures_configured_bot_computer(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, object]] = []

    class _Manager:
        def own_agent_id(self) -> str:
            return "groken-id"

        def command(self, method: str, args: object) -> dict[str, object]:
            commands.append((method, args))
            if method == "getForeverBoxStatus":
                return {"agentId": "groken-id", "state": "absent", "vncUrl": None}
            return {
                "agentId": "groken-id",
                "state": "running",
                "vncUrl": "http://127.0.0.1:6081/vnc.html?path=websockify%3Ftoken%3D5",
            }

        def ensure_sandbox_metadata(self) -> dict[str, str]:
            return {
                "vncUrl": "https://tenant-pod-6080.example/vnc.html",
                "forkVncBaseUrl": "https://tenant-pod-6081.example",
                "networkToken": "secret",
                "podId": "pod",
            }

    class _Stop(Exception):
        pass

    monkeypatch.setattr(cli, "_manager", lambda: _Manager())
    monkeypatch.setattr("groken.vnc_proxy.serve_vnc_proxy", lambda _url: (_ for _ in ()).throw(_Stop))

    with pytest.raises(_Stop):
        cli.cmd_vnc(False)

    assert commands == [
        ("getForeverBoxStatus", {"id": "groken-id"}),
        ("ensureForeverBox", {"id": "groken-id"}),
    ]
