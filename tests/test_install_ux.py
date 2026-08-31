import importlib.util
import json
import sys
from pathlib import Path
from typing import TypeGuard, cast

import pytest

from groken import cli
from groken.installers import (
    INSTALLERS,
    _install_opencode,
    _uninstall_opencode,
    detected_agents,
    install_all,
    install_json_mcp,
    uninstall_all,
    uninstall_json_mcp,
)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _require_object_dict(value: object) -> dict[str, object]:
    if not _is_object_dict(value):
        raise TypeError("expected JSON object")
    return value


def _load_json_object(path: Path) -> dict[str, object]:
    loaded = cast(object, json.loads(path.read_text()))
    return _require_object_dict(loaded)


def _present() -> bool:
    return True


def _absent() -> bool:
    return False


def _installed(_dry_run: bool) -> str:
    return "ok"


def test_detected_agents_returns_only_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(INSTALLERS, "fake-present", (_present, _installed))
    monkeypatch.setitem(INSTALLERS, "fake-absent", (_absent, _installed))
    found = detected_agents()
    assert "fake-present" in found
    assert "fake-absent" not in found


def test_uninstall_removes_only_groken_entry(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    _ = cfg.write_text(
        json.dumps(
            {"mcpServers": {"other": {"command": "x"}, "groken": {"command": "y"}}}
        )
    )
    _ = uninstall_json_mcp(cfg, key="mcpServers", dry_run=False)
    data = _load_json_object(cfg)
    servers = _require_object_dict(data["mcpServers"])
    assert list(servers) == ["other"]


def _opencode_config(home: Path, name: str) -> Path:
    config = home / ".config" / "opencode" / name
    config.parent.mkdir(parents=True, exist_ok=True)
    return config


def test_opencode_jsonc_install_is_idempotent_and_preserves_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    config = _opencode_config(tmp_path, "opencode.jsonc")
    original = """// top-level comment
{
  "mcp": {
    "other": {"type": "local", "command": ["keep"]}, // sibling comment
  },
}
"""
    _ = config.write_text(original)

    # When
    first_result = _install_opencode(False)
    first_install = config.read_text()
    second_result = _install_opencode(False)

    # Then
    assert first_result == f"installed -> {config}"
    assert second_result == f"already present -> {config}"
    assert config.read_text() == first_install
    assert "// top-level comment" in first_install
    assert "// sibling comment" in first_install
    assert '"mcpServers"' not in first_install
    assert first_install.count('"groken"') == 1
    assert config.with_suffix(".jsonc.groken-bak").read_text() == original


def test_opencode_jsonc_uninstall_removes_only_groken_and_preserves_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    config = _opencode_config(tmp_path, "opencode.jsonc")
    original = """// top-level comment
{
  "mcp": {
    "groken": {"type": "local", "command": ["groken-mcp"]},
    "other": {"type": "local", "command": ["keep"]}, // sibling comment
  },
}
"""
    _ = config.write_text(original)

    # When
    result = _uninstall_opencode(False)

    # Then
    text = config.read_text()
    assert result == f"removed <- {config}"
    assert '"groken"' not in text
    assert '"other"' in text
    assert "// top-level comment" in text
    assert "// sibling comment" in text
    assert config.with_suffix(".jsonc.groken-bak").read_text() == original


def test_opencode_cli_dry_run_with_both_configs_selects_jsonc_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    jsonc = _opencode_config(tmp_path, "opencode.jsonc")
    plain = _opencode_config(tmp_path, "opencode.json")
    jsonc_original = '// keep\n{"mcp": {}}\n'
    plain_original = '{"mcp": {}}\n'
    _ = jsonc.write_text(jsonc_original)
    _ = plain.write_text(plain_original)
    monkeypatch.setattr(sys, "argv", ["groken", "install", "--dry-run", "--all"])

    # When
    cli.main()

    # Then
    output = capsys.readouterr().out
    assert f"would write mcp.groken -> {jsonc}" in output
    assert f"would write mcp.groken -> {plain}\n" not in output
    assert jsonc.read_text() == jsonc_original
    assert plain.read_text() == plain_original
    assert list(jsonc.parent.glob("*.groken-bak")) == []


def test_opencode_install_all_with_both_configs_writes_only_jsonc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    jsonc = _opencode_config(tmp_path, "opencode.jsonc")
    plain = _opencode_config(tmp_path, "opencode.json")
    jsonc_original = '// keep\n{"mcp": {}}\n'
    plain_original = b'{"mcp": {}}\n'
    _ = jsonc.write_text(jsonc_original)
    _ = plain.write_bytes(plain_original)
    plain_mtime = plain.stat().st_mtime_ns

    # When
    result = install_all(False, only=["opencode"])["opencode"]

    # Then
    assert result == f"installed -> {jsonc}"
    assert "// keep" in jsonc.read_text()
    assert '"groken"' in jsonc.read_text()
    assert plain.read_bytes() == plain_original
    assert plain.stat().st_mtime_ns == plain_mtime
    assert jsonc.with_suffix(".jsonc.groken-bak").exists()
    assert not plain.with_suffix(".json.groken-bak").exists()


def test_opencode_jsonc_malformed_input_is_not_overwritten_or_backed_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    config = _opencode_config(tmp_path, "opencode.jsonc")
    original = b"{ malformed // keep"
    _ = config.write_bytes(original)

    # When
    result = _install_opencode(False)

    # Then
    assert result == f"skipped (unparseable JSON): {config}"
    assert config.read_bytes() == original
    assert not config.with_suffix(".jsonc.groken-bak").exists()


def test_opencode_json_only_keeps_mcp_command_array_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    config = _opencode_config(tmp_path, "opencode.json")
    _ = config.write_text('{"mcp": {"other": {"command": ["keep"]}}}\n')

    # When
    result = _install_opencode(False)

    # Then
    data = _load_json_object(config)
    mcp = _require_object_dict(data["mcp"])
    groken = _require_object_dict(mcp["groken"])
    assert result == f"installed -> {config}"
    assert groken["type"] == "local"
    assert groken["command"] == [str(Path(sys.executable).parent / "groken-mcp")]
    assert groken["enabled"] is True


def test_uninstall_is_safe_when_absent(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    _ = cfg.write_text(json.dumps({"mcpServers": {"other": {}}}))
    result = uninstall_json_mcp(cfg, key="mcpServers", dry_run=False)
    data = _load_json_object(cfg)
    servers = _require_object_dict(data["mcpServers"])
    assert "not present" in result
    assert list(servers) == ["other"]


def test_jsonc_garbage_is_skipped(tmp_path: Path) -> None:
    cfg = tmp_path / "opencode.jsonc"
    _ = cfg.write_text("not jsonc")
    assert "unparseable" in install_json_mcp(cfg, "mcpServers", "x", False, jsonc=True)


def test_install_all_requires_explicit_selection() -> None:
    with pytest.raises(ValueError):
        _ = install_all(dry_run=True, only=[])


def test_uninstall_all_runs_only_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def uninstall_a(_dry_run: bool) -> str:
        ran.append("a")
        return "removed"

    def uninstall_b(_dry_run: bool) -> str:
        ran.append("b")
        return "removed"

    monkeypatch.setitem(INSTALLERS, "fake-a", (_present, _installed))
    monkeypatch.setitem(INSTALLERS, "fake-b", (_present, _installed))
    monkeypatch.setattr(
        "groken.installers.UNINSTALLERS",
        {
            "fake-a": uninstall_a,
            "fake-b": uninstall_b,
        },
    )
    results = uninstall_all(dry_run=False, only=["fake-a"])
    assert ran == ["a"]
    assert results == {"fake-a": "removed"}


def test_bare_groken_shows_guide_not_argparse_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["groken"])
    cli.main()
    out = capsys.readouterr().out
    assert "groken" in out.lower()
    assert "install" in out
    assert "ask" in out


def test_guide_command_prints_canonical_first_run_guide(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["groken", "guide"])

    cli.main()

    assert capsys.readouterr().out == f"{cli.GUIDE}\n"


def test_install_explicit_target_bypasses_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    selected: list[list[str]] = []

    def fake_install_cli_command(dry_run: bool) -> str:
        return "already present"

    monkeypatch.setattr(
        "groken.installers.install_cli_command", fake_install_cli_command
    )

    def fail_detection() -> list[str]:
        pytest.fail("explicit targets must not invoke automatic detection")

    def fake_install_all(*, dry_run: bool, only: list[str]) -> dict[str, str]:
        assert dry_run is True
        selected.append(only)
        return {only[0]: "would install"}

    monkeypatch.setattr("groken.installers.detected_agents", fail_detection)
    monkeypatch.setattr("groken.installers.install_all", fake_install_all)

    # When
    cli.cmd_install(["cursor-skills"], dry_run=True, use_all=False)

    # Then
    assert selected == [["cursor-skills"]]


def test_explicit_mcp_target_fails_without_optional_dependency_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def missing_spec(_name: str) -> None:
        return None

    def existing_cli(*, dry_run: bool) -> str:
        assert dry_run is False
        return "already present"

    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", missing_spec)
    monkeypatch.setattr("groken.installers.install_cli_command", existing_cli)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    # When
    with pytest.raises(SystemExit) as raised:
        cli.cmd_install(["opencode"], dry_run=False, use_all=False)

    # Then
    assert "groken[mcp]" in str(raised.value)
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()


def test_install_exits_nonzero_when_skill_source_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def fake_install_cli_command(dry_run: bool) -> str:
        return "already present"

    def fake_install_all(*, dry_run: bool, only: list[str]) -> dict[str, str]:
        assert dry_run is True
        return {only[0]: "failed (no skill source at /missing/SKILL.md)"}

    monkeypatch.setattr(
        "groken.installers.install_cli_command", fake_install_cli_command
    )
    monkeypatch.setattr("groken.installers.install_all", fake_install_all)

    # When
    with pytest.raises(SystemExit) as raised:
        cli.cmd_install(["cursor-skills"], dry_run=True, use_all=False)

    # Then
    assert raised.value.code == (
        "install failed: cursor-skills: failed (no skill source at /missing/SKILL.md)"
    )


def test_install_ensures_global_cli_before_agent_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def fake_install_cli_command(dry_run: bool) -> str:
        calls.append(dry_run)
        return "would install"

    monkeypatch.setattr(
        "groken.installers.install_cli_command", fake_install_cli_command
    )
    monkeypatch.setattr("groken.installers.detected_agents", list)

    cli.cmd_install([], dry_run=True, use_all=True)

    assert calls == [True]


def test_configure_ensures_global_cli_after_selecting_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Manager:
        def command(self, method: str) -> list[dict[str, object]]:
            assert method == "listAgents"
            return [{"id": "bot-1", "name": "primary"}]

    calls: list[bool] = []
    remembered: list[tuple[str, str]] = []

    def fake_remember_bot(bot_id: str, name: str) -> None:
        remembered.append((bot_id, name))

    def fake_install_cli_command(dry_run: bool) -> str:
        calls.append(dry_run)
        return "installed"

    monkeypatch.setattr(cli, "_manager", Manager)
    monkeypatch.setattr("groken.config.remember_bot", fake_remember_bot)
    monkeypatch.setattr(
        "groken.installers.install_cli_command", fake_install_cli_command
    )

    cli.cmd_configure("primary")

    assert remembered == [("bot-1", "primary")]
    assert calls == [False]


def test_install_non_tty_without_selection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "groken.installers.install_cli_command", lambda dry_run: "already present"
    )
    monkeypatch.setattr(sys, "argv", ["groken", "install"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert "--all" in str(exc.value)


def test_install_help_lists_every_registered_target(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from groken.installers import INSTALLERS

    monkeypatch.setattr(sys, "argv", ["groken", "install", "--help"])

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert all(name in help_text for name in INSTALLERS)
