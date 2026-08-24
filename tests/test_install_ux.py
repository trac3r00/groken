import json
import sys

import pytest

from groken import cli
from groken.installers import (
    INSTALLERS,
    detected_agents,
    install_all,
    uninstall_all,
    install_json_mcp,
    uninstall_json_mcp,
)


def test_detected_agents_returns_only_present(monkeypatch):
    monkeypatch.setitem(INSTALLERS, "fake-present", (lambda: True, lambda dry_run: "ok"))
    monkeypatch.setitem(INSTALLERS, "fake-absent", (lambda: False, lambda dry_run: "ok"))
    found = detected_agents()
    assert "fake-present" in found
    assert "fake-absent" not in found


def test_uninstall_removes_only_groken_entry(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}, "groken": {"command": "y"}}}))
    uninstall_json_mcp(cfg, key="mcpServers", dry_run=False)
    data = json.loads(cfg.read_text())
    assert list(data["mcpServers"]) == ["other"]


def test_opencode_jsonc_install_uninstall_preserves_comments(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    original = '''// my comment
{
  "mcpServers": {
    "other": {"command": "keep"}, // trailing after
  },
}
'''
    cfg.write_text(original)
    install_json_mcp(cfg, "mcpServers", "groken --mcp", False, jsonc=True)
    text = cfg.read_text()
    assert '"other": {"command": "keep"}' in text
    assert "// my comment" in text and "// trailing after" in text
    assert '"groken"' in text
    install_json_mcp(cfg, "mcpServers", "groken --mcp", False, jsonc=True)
    assert cfg.read_text().count('"groken"') == 1
    uninstall_json_mcp(cfg, "mcpServers", False, jsonc=True)
    text = cfg.read_text()
    assert '"groken"' not in text and '"other": {"command": "keep"}' in text
    assert "// my comment" in text and "// trailing after" in text


def test_uninstall_is_safe_when_absent(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {}}}))
    result = uninstall_json_mcp(cfg, key="mcpServers", dry_run=False)
    assert "not present" in result
    assert list(json.loads(cfg.read_text())["mcpServers"]) == ["other"]


def test_jsonc_garbage_is_skipped(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text("not jsonc")
    assert "unparseable" in install_json_mcp(cfg, "mcpServers", "x", False, jsonc=True)


def test_install_all_requires_explicit_selection():
    with pytest.raises(ValueError):
        install_all(dry_run=True, only=[])


def test_uninstall_all_runs_only_selected(monkeypatch):
    ran = []
    monkeypatch.setitem(INSTALLERS, "fake-a", (lambda: True, lambda dry_run: "ok"))
    monkeypatch.setitem(INSTALLERS, "fake-b", (lambda: True, lambda dry_run: "ok"))
    monkeypatch.setattr("groken.installers.UNINSTALLERS", {
        "fake-a": lambda dry_run: ran.append("a") or "removed",
        "fake-b": lambda dry_run: ran.append("b") or "removed",
    })
    results = uninstall_all(dry_run=False, only=["fake-a"])
    assert ran == ["a"]
    assert results == {"fake-a": "removed"}


def test_bare_groken_shows_guide_not_argparse_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["groken"])
    cli.main()
    out = capsys.readouterr().out
    assert "groken" in out.lower()
    assert "install" in out
    assert "ask" in out


def test_install_non_tty_without_selection_errors(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["groken", "install"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert "--all" in str(exc.value)
