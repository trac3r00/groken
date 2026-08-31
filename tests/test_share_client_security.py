import builtins
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from groken import cli, share_client, share_store
from groken.exec_service import ExecResult
from groken.share_client import (
    RelayManager,
    ShareLink,
    SharePermissionError,
    ShareProtocolError,
    ShareRemoteError,
)


class FakeOwnerManager:
    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        assert method == "listAgents"
        assert args is None
        return [
            {"id": "bot-id", "name": "Shared Bot"},
            {"id": "other-id", "name": "Other Bot"},
        ]


def test_relay_exec_and_vnc_use_proxy_endpoints_only() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/exec":
            assert json.loads(request.content) == {
                "command": "pwd",
                "cwd": "/work",
                "timeout_ms": 3210,
            }
            return httpx.Response(
                200,
                json={"stdout": "out", "stderr": "err", "exit_code": 7},
            )
        if request.url.path == "/v1/vnc":
            assert json.loads(request.content) == {}
            return httpx.Response(200, json={"url": "https://vnc.example.test/short"})
        return httpx.Response(404)

    manager = RelayManager(
        ShareLink("https://relay.example.test", "share-token"),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert manager.execute("pwd", "/work", 3210) == ExecResult("out", "err", 7)
    assert manager.vnc_url() == "https://vnc.example.test/short"
    assert seen == ["/v1/exec", "/v1/vnc"]
    assert "/v1/metadata" not in seen


def test_other_bot_selector_raises_typed_share_permission_error() -> None:
    manager = RelayManager(
        ShareLink("https://relay.example.test", "share-token"),
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json={"agent_id": "shared-id", "name": "shared"}
                )
            )
        ),
    )

    with pytest.raises(SharePermissionError, match="unavailable through this share"):
        manager.resolve_agent("private-bot")


def test_ask_and_exec_use_operation_specific_read_timeouts() -> None:
    reads: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions["timeout"]
        assert isinstance(timeout, dict)
        read = timeout.get("read")
        assert isinstance(read, float)
        reads.append(read)
        if request.url.path == "/v1/ask":
            return httpx.Response(200, json={"reply": "answer"})
        return httpx.Response(200, json={"stdout": "", "stderr": "", "exit_code": 0})

    manager = RelayManager(
        ShareLink("https://relay.example.test", "share-token"),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert manager.ask("bot-id", "question", timeout_s=600) == "answer"
    assert manager.execute("pwd", timeout_ms=120_000).exit_code == 0
    assert reads[0] >= 630
    assert reads[1] >= 150


def test_ask_stream_sends_idle_timeout_and_requires_done() -> None:
    chunks: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "text": "question",
            "timeout_s": 70,
            "idle_s": 11,
        }
        return httpx.Response(
            200,
            text=(
                'event: chunk\ndata: {"text": "partial"}\n\n'
                'event: done\ndata: {"reply": "complete"}\n\n'
            ),
        )

    manager = RelayManager(
        ShareLink("https://relay.example.test", "share-token"),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert (
        manager.ask_stream(
            "bot-id", "question", timeout_s=70, idle_s=11, on_chunk=chunks.append
        )
        == "complete"
    )
    assert chunks == ["partial"]


def test_ask_stream_rejects_truncated_or_error_frames() -> None:
    responses = iter(
        [
            httpx.Response(200, text='event: chunk\ndata: {"text": "partial"}\n\n'),
            httpx.Response(200, text='event: error\ndata: {"detail": "revoked"}\n\n'),
        ]
    )
    manager = RelayManager(
        ShareLink("https://relay.example.test", "share-token"),
        http=httpx.Client(
            transport=httpx.MockTransport(lambda _request: next(responses))
        ),
    )

    with pytest.raises(ShareProtocolError, match="before done"):
        manager.ask_stream("bot-id", "question")
    with pytest.raises(ShareRemoteError, match="revoked"):
        manager.ask_stream("bot-id", "question")


def test_stream_transport_errors_become_typed_share_errors() -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("quiet stream")

    manager = RelayManager(
        ShareLink("https://relay.example.test", "share-token"),
        http=httpx.Client(transport=httpx.MockTransport(fail)),
    )

    with pytest.raises(ShareRemoteError, match="share relay stream failed"):
        manager.ask_stream("bot-id", "question")
    with pytest.raises(ShareRemoteError, match="share relay stream failed"):
        list(manager.events())


def test_owner_create_pins_resolved_bot_id(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(share_store, "_DEFAULT_PATH", tmp_path / "shares.json")

    cli.cmd_share_create("alice", "Shared Bot", manager=FakeOwnerManager())

    assert share_store.ShareStore().list() == [
        share_store.ShareRecord("alice", "bot-id", "Shared Bot")
    ]
    output = capsys.readouterr().out
    assert "Shared Bot (bot-id)" in output


def test_connect_reads_private_file_and_rejects_public_http(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(share_client, "_DEFAULT_PATH", tmp_path / "share.json")
    token_file = tmp_path / "token"
    token_file.write_text("share-token\n")
    token_file.chmod(0o600)

    cli.cmd_share_connect("https://relay.example.test", token_file=str(token_file))

    assert share_client.load_share_link() == ShareLink(
        "https://relay.example.test", "share-token"
    )
    assert "share-token" not in capsys.readouterr().out

    with pytest.raises(SystemExit, match="HTTPS"):
        cli.cmd_share_connect("http://relay.example.test", token_file=str(token_file))

    token_file.chmod(0o644)
    with pytest.raises(SystemExit, match="0600"):
        cli.cmd_share_connect("https://relay.example.test", token_file=str(token_file))


def test_share_mode_blocks_local_account_commands_before_dispatch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(share_client, "_DEFAULT_PATH", tmp_path / "share.json")
    share_client.save_share_link(ShareLink("https://relay.example.test", "share-token"))
    monkeypatch.setattr(
        cli,
        "cmd_doctor",
        lambda: pytest.fail("blocked command reached local account client"),
    )
    monkeypatch.setattr(sys, "argv", ["groken", "doctor"])

    with pytest.raises(SystemExit, match="unavailable while connected to a share"):
        cli._main_impl()


def test_share_serve_reports_bounded_install_hint_when_dependency_is_missing(
    monkeypatch: Any,
) -> None:
    real_import = builtins.__import__

    def import_without_uvicorn(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "uvicorn":
            raise ModuleNotFoundError(name="uvicorn")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_uvicorn)

    with pytest.raises(SystemExit) as raised:
        cli.cmd_share_serve("127.0.0.1", 8787)

    assert isinstance(raised.value.code, str)
    assert ".[share]" in raised.value.code
    assert len(raised.value.code) <= 160


def test_connect_parser_has_no_positional_token(monkeypatch: Any) -> None:
    captured: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        cli,
        "cmd_share_connect",
        lambda url, token_file=None: captured.append((url, token_file)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["groken", "share", "connect", "https://relay.example.test"],
    )

    cli._main_impl()

    assert captured == [("https://relay.example.test", None)]
