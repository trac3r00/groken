import sys
from collections.abc import Callable

import pytest
from _pytest.capture import CaptureResult

from groken import cli


class FakeManager:
    def resolve_agent(self, agent: str | None) -> str:
        return agent or "a1"

    def ask(self, agent: str, text: str, timeout_s: float) -> str:
        _ = agent, text, timeout_s
        return "one two"

    def ask_stream(
        self,
        agent: str,
        text: str,
        timeout_s: float,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        _ = agent, text, timeout_s, idle_s
        assert on_chunk is not None
        for chunk in ("one", " ", "two"):
            on_chunk(chunk)
        return "one two"


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isatty: bool,
) -> CaptureResult[str]:
    monkeypatch.setattr(cli, "_manager", FakeManager)
    monkeypatch.setattr(sys, "argv", ["groken", "ask", "hello", "--stream"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)
    cli.main()
    return capsys.readouterr()


def test_stream_emits_chunks_in_order_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = run(monkeypatch, capsys, True)
    assert captured.out == "one two"


def test_stream_keeps_stdout_pure_when_piped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = run(monkeypatch, capsys, False)
    assert captured.out == "one two\n"
    assert captured.err == ""
