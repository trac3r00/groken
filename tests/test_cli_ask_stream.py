import sys

from groken import cli


class FakeManager:
    def resolve_agent(self, agent):
        return agent or "a1"

    def ask(self, agent, text, timeout_s):
        return "one two"

    def ask_stream(self, agent, text, timeout_s, on_chunk):
        for chunk in ("one", " ", "two"):
            on_chunk(chunk)
        return "one two"


def run(monkeypatch, capsys, isatty):
    monkeypatch.setattr(cli, "_manager", FakeManager)
    monkeypatch.setattr(sys, "argv", ["groken", "ask", "hello", "--stream"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)
    cli.main()
    return capsys.readouterr()


def test_stream_emits_chunks_in_order_on_tty(monkeypatch, capsys):
    captured = run(monkeypatch, capsys, True)
    assert captured.out == "one two"


def test_stream_keeps_stdout_pure_when_piped(monkeypatch, capsys):
    captured = run(monkeypatch, capsys, False)
    assert captured.out == "one two\n"
    assert captured.err == ""
