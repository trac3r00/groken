import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path
from typing import TypeGuard, cast

import pytest

from groken.installers import (
    INSTALLERS,
    UNINSTALLERS,
    detected_agents,
    install_all,
    install_cli_command,
    install_json_mcp,
    install_skill_dir,
    install_toml_mcp,
    install_yaml_mcp_hermes,
    skill_source,
    uninstall_all,
    uninstall_json_mcp,
    uninstall_toml_mcp,
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


def test_install_cli_command_uses_editable_uv_tool_with_all_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = Path(__file__).resolve().parent.parent
    calls: list[tuple[list[str], Path]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/uv" if name == "uv" else None

    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    monkeypatch.setattr("groken.installers.shutil.which", fake_which)
    monkeypatch.setattr("groken.installers.subprocess.run", fake_run)

    result = install_cli_command(dry_run=False)

    assert calls == [
        (
            [
                "/usr/bin/uv",
                "tool",
                "install",
                "--editable",
                ".[mcp,share,worker]",
            ],
            project_root,
        )
    ]
    assert result == f"installed -> {tmp_path / '.local' / 'bin' / 'groken'}"


def test_install_cli_command_uses_current_wheel_entrypoint_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "site-packages"
    executable = tmp_path / "venv" / "bin" / "python"
    entrypoint = executable.with_name("groken")
    entrypoint.parent.mkdir(parents=True)
    entrypoint.touch()
    monkeypatch.setattr("groken.installers.PROJECT_ROOT", project_root)
    monkeypatch.setattr("groken.installers.HOME", tmp_path / "home")
    monkeypatch.setattr("groken.installers.sys.executable", str(executable))

    result = install_cli_command(dry_run=False)

    assert result == f"already present -> {entrypoint}"


def test_json_mcp_merge_is_idempotent_and_preserves_siblings(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    _ = cfg.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "unrelated": 1})
    )
    for _ in range(2):
        _ = install_json_mcp(
            cfg, key="mcpServers", command="/bin/groken-mcp", dry_run=False
        )
    data = _load_json_object(cfg)
    servers = _require_object_dict(data["mcpServers"])
    other = _require_object_dict(servers["other"])
    groken = _require_object_dict(servers["groken"])
    assert data["unrelated"] == 1
    assert other == {"command": "x"}
    assert groken["command"] == "/bin/groken-mcp"
    assert len(servers) == 2


def test_json_mcp_creates_file_when_absent(tmp_path: Path) -> None:
    cfg = tmp_path / "nested" / "mcp.json"
    _ = install_json_mcp(cfg, key="servers", command="/bin/groken-mcp", dry_run=False)
    data = _load_json_object(cfg)
    servers = _require_object_dict(data["servers"])
    groken = _require_object_dict(servers["groken"])
    assert groken["command"] == "/bin/groken-mcp"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    _ = install_json_mcp(cfg, key="mcpServers", command="/bin/groken-mcp", dry_run=True)
    assert not cfg.exists()


def test_jsonc_install_ignores_commented_keys_and_braces(tmp_path: Path) -> None:
    # Given
    cfg = tmp_path / "opencode.jsonc"
    original = """{
  // \"mcp\": { \"decoy\": true },
  \"unrelated\": {\"keep\": true},
  \"mcp\": {
    \"other\": {\"command\": \"keep\"},
    /* "groken": {"command": "decoy"}, closing-brace decoys: }}} */
  }
}
"""
    _ = cfg.write_text(original)

    # When
    first = install_json_mcp(
        cfg, key="mcp", command="/bin/groken-mcp", dry_run=False, jsonc=True
    )
    second = install_json_mcp(
        cfg, key="mcp", command="/bin/groken-mcp", dry_run=False, jsonc=True
    )

    # Then
    updated = cfg.read_text()
    assert first == f"installed -> {cfg}"
    assert second == f"already present -> {cfg}"
    assert '// "mcp": { "decoy": true },' in updated
    assert (
        '/* "groken": {"command": "decoy"}, closing-brace decoys: }}} */'
        in updated
    )
    assert '"unrelated": {"keep": true}' in updated
    assert '"other": {"command": "keep"}' in updated
    assert updated.count('"groken":') == 2


def test_jsonc_uninstall_handles_escaped_groken_key(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    _ = cfg.write_text(
        '{"mcpServers":{"gro\\u006ben":{"command":"x"},"other":{"command":"y"}}}'
    )

    result = uninstall_json_mcp(cfg, key="mcpServers", dry_run=False, jsonc=True)

    assert result == f"removed <- {cfg}"
    assert _load_json_object(cfg) == {"mcpServers": {"other": {"command": "y"}}}


def test_opencode_shape_uses_command_array(tmp_path: Path) -> None:
    cfg = tmp_path / "opencode.json"
    _ = install_json_mcp(
        cfg,
        key="mcp",
        command="/bin/groken-mcp",
        dry_run=False,
        entry={"type": "local", "command": ["/bin/groken-mcp"], "enabled": True},
    )
    data = _load_json_object(cfg)
    mcp = _require_object_dict(data["mcp"])
    entry = _require_object_dict(mcp["groken"])
    assert entry["command"] == ["/bin/groken-mcp"]
    assert entry["enabled"] is True


def test_toml_mcp_merge_is_idempotent_and_preserves_siblings(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _ = cfg.write_text('model = "gpt"\n\n[mcp_servers.other]\ncommand = "x"\n')
    for _ in range(2):
        _ = install_toml_mcp(cfg, command="/bin/groken-mcp", dry_run=False)
    text = cfg.read_text()
    assert text.count("[mcp_servers.groken]") == 1
    assert "[mcp_servers.other]" in text
    assert 'model = "gpt"' in text
    assert "/bin/groken-mcp" in text


def test_toml_mcp_serializes_command_as_a_safe_string(tmp_path: Path) -> None:
    # Given
    cfg = tmp_path / "config.toml"
    command = 'C:\\tools\\groken"mcp\n# not a TOML comment'

    # When
    _ = install_toml_mcp(cfg, command=command, dry_run=False)

    # Then
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["mcp_servers"]["groken"]["command"] == command


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_toml_mcp_refuses_malformed_existing_config_without_backup(
    tmp_path: Path, operation: str
) -> None:
    # Given
    cfg = tmp_path / "config.toml"
    original = '[mcp_servers.groken]\ncommand = "unterminated\n'
    _ = cfg.write_text(original)

    # When
    result = (
        install_toml_mcp(cfg, command="/bin/groken-mcp", dry_run=False)
        if operation == "install"
        else uninstall_toml_mcp(cfg, dry_run=False)
    )

    # Then
    assert result == f"skipped (unparseable TOML): {cfg}"
    assert cfg.read_text() == original
    assert not cfg.with_suffix(".toml.groken-bak").exists()


def test_toml_install_ignores_header_text_inside_values_and_comments(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    original = (
        'notice = """\n[mcp_servers.groken]\n"""\n'
        '# [mcp_servers.groken]\n\n'
        '[mcp_servers.other]\ncommand = "keep"\n'
    )
    cfg.write_text(original)

    result = install_toml_mcp(cfg, command="/safe/groken-mcp", dry_run=False)

    parsed = tomllib.loads(cfg.read_text())
    assert result == f"installed -> {cfg}"
    assert parsed["notice"] == "[mcp_servers.groken]\n"
    assert parsed["mcp_servers"]["other"]["command"] == "keep"
    assert parsed["mcp_servers"]["groken"]["command"] == "/safe/groken-mcp"


def test_toml_uninstall_ignores_header_text_inside_values_and_comments(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    original = (
        'notice = "[mcp_servers.groken]"\n'
        '# [mcp_servers.groken]\n\n'
        '[mcp_servers.other]\ncommand = "keep"\n\n'
        '[mcp_servers.groken]\ncommand = "/safe/groken-mcp"\nargs = []\n'
    )
    cfg.write_text(original)

    result = uninstall_toml_mcp(cfg, dry_run=False)

    parsed = tomllib.loads(cfg.read_text())
    assert result == f"removed <- {cfg}"
    assert parsed["notice"] == "[mcp_servers.groken]"
    assert parsed["mcp_servers"] == {"other": {"command": "keep"}}


def test_yaml_mcp_serializes_command_as_a_safe_scalar(tmp_path: Path) -> None:
    # Given
    cfg = tmp_path / "config.yaml"
    command = 'C:\\tools\\groken"mcp\n# not a YAML comment'
    _ = cfg.write_text("mcp_servers:\n  sibling:\n    command: keep # keep comment\n")

    # When
    _ = install_yaml_mcp_hermes(cfg, command=command, dry_run=False)

    # Then
    command_line = next(
        line
        for line in cfg.read_text().splitlines()
        if line.startswith("    command: ")
    )
    assert json.loads(command_line.removeprefix("    command: ")) == command
    assert "command: keep # keep comment" in cfg.read_text()


def test_skill_source_prefers_checkout_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    checkout = tmp_path / "checkout"
    prefix = tmp_path / "prefix"
    checkout_source = checkout / "skill" / "SKILL.md"
    checkout_source.parent.mkdir(parents=True)
    _ = checkout_source.write_text("checkout")
    prefix_source = prefix / "skill" / "SKILL.md"
    prefix_source.parent.mkdir(parents=True)
    _ = prefix_source.write_text("wheel")
    monkeypatch.setattr("groken.installers.PROJECT_ROOT", checkout)
    monkeypatch.setattr("groken.installers.sys.prefix", str(prefix))

    # When
    source = skill_source()

    # Then
    assert source == checkout_source


def test_skill_source_falls_back_to_installed_wheel_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    checkout = tmp_path / "checkout"
    prefix_source = tmp_path / "prefix" / "skill" / "SKILL.md"
    prefix_source.parent.mkdir(parents=True)
    _ = prefix_source.write_text("wheel")
    monkeypatch.setattr("groken.installers.PROJECT_ROOT", checkout)
    monkeypatch.setattr("groken.installers.sys.prefix", str(tmp_path / "prefix"))

    # When
    source = skill_source()

    # Then
    assert source == prefix_source


def test_skill_dir_missing_source_fails_dry_run(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "missing" / "SKILL.md"

    # When
    result = install_skill_dir(tmp_path / "skills", source, dry_run=True)

    # Then
    assert result == f"failed (no skill source at {source})"


def test_skill_dir_install_copies_skill(tmp_path: Path) -> None:
    src = tmp_path / "SKILL.md"
    _ = src.write_text("---\nname: groken\n---\nbody")
    dest_root = tmp_path / "skills"
    _ = install_skill_dir(dest_root, src, dry_run=False)
    assert (dest_root / "groken" / "SKILL.md").read_text().startswith("---")


def test_install_all_explicit_targets_bypass_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    seen: list[str] = []

    def fake_installer(_dry_run: bool) -> str:
        seen.append("ran")
        return "installed"

    monkeypatch.setattr(
        "groken.installers.INSTALLERS",
        {
            "fake-present": (lambda: True, fake_installer),
            "fake-absent": (lambda: False, fake_installer),
        },
    )

    # When
    results = install_all(dry_run=False, only=["fake-present", "fake-absent"])

    # Then
    assert results == {"fake-present": "installed", "fake-absent": "installed"}
    assert seen == ["ran", "ran"]


def test_install_all_automatic_selection_detects_harness_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    seen: list[str] = []

    def fake_installer(_dry_run: bool) -> str:
        seen.append("ran")
        return "installed"

    monkeypatch.setattr(
        "groken.installers.INSTALLERS",
        {
            "fake-present": (lambda: True, fake_installer),
            "fake-absent": (lambda: False, fake_installer),
        },
    )

    # When
    results = install_all(dry_run=True)

    # Then
    assert results == {
        "fake-present": "installed",
        "fake-absent": "skipped (not installed)",
    }
    assert seen == ["ran"]


def test_install_all_rejects_unknown_agent() -> None:
    with pytest.raises(ValueError):
        _ = install_all(dry_run=True, only=["nope-not-real"])


def test_new_harness_targets_are_registered() -> None:
    assert {"gjc", "gjc-skills", "openclaw", "omo-skill"}.issubset(INSTALLERS)
    assert {"gjc", "gjc-skills", "openclaw", "omo-skill"}.issubset(UNINSTALLERS)


@pytest.mark.parametrize(
    ("target", "harness_root"),
    [
        ("omo", Path(".agents")),
        ("omo-skill", Path(".agents")),
        ("claude-skills", Path(".claude")),
        ("codex-skills", Path(".codex")),
        ("cursor-skills", Path(".cursor")),
        ("gjc-skills", Path(".gjc")),
    ],
)
def test_skill_harness_root_is_detected_before_skills_directory_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    harness_root: Path,
) -> None:
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    (tmp_path / harness_root).mkdir(parents=True)

    assert target in detected_agents()


@pytest.mark.parametrize(
    ("target", "relative_root"),
    [
        ("cursor-skills", Path(".cursor/skills")),
        ("gjc-skills", Path(".gjc/agent/skills")),
    ],
)
def test_skill_harness_uses_portable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    relative_root: Path,
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    root = tmp_path / relative_root
    root.mkdir(parents=True)

    # When
    result = install_all(False, only=[target])[target]

    # Then
    destination = root / "groken" / "SKILL.md"
    assert result == f"installed -> {destination}"
    assert destination.read_bytes() == Path("skill/SKILL.md").read_bytes()


def test_skill_only_target_installs_without_mcp_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    def missing_spec(_name: str) -> None:
        return None

    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", missing_spec)

    # When
    result = install_all(False, only=["cursor-skills"])["cursor-skills"]

    # Then
    destination = tmp_path / ".cursor" / "skills" / "groken" / "SKILL.md"
    assert result == f"installed -> {destination}"
    assert destination.read_bytes() == Path("skill/SKILL.md").read_bytes()


def test_cursor_skill_install_mirrors_detected_legacy_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    legacy_root = tmp_path / ".cursor" / "skills-cursor"
    legacy_root.mkdir(parents=True)

    # When
    result = install_all(False, only=["cursor-skills"])["cursor-skills"]

    # Then
    canonical = tmp_path / ".cursor" / "skills" / "groken" / "SKILL.md"
    legacy = legacy_root / "groken" / "SKILL.md"
    assert "installed" in result
    assert canonical.read_bytes() == Path("skill/SKILL.md").read_bytes()
    assert legacy.read_bytes() == canonical.read_bytes()


def test_cursor_skill_uninstall_removes_canonical_and_legacy_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    canonical = tmp_path / ".cursor" / "skills" / "groken" / "SKILL.md"
    legacy = tmp_path / ".cursor" / "skills-cursor" / "groken" / "SKILL.md"
    canonical.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    _ = canonical.write_text("canonical")
    _ = legacy.write_text("legacy")

    # When
    result = uninstall_all(False, only=["cursor-skills"])["cursor-skills"]

    # Then
    assert "removed" in result
    assert not canonical.parent.exists()
    assert not legacy.parent.exists()


def test_omo_skill_dry_run_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    result = INSTALLERS["omo-skill"][1](True)
    assert "would copy skill" in result
    assert not (tmp_path / ".agents" / "skills" / "groken" / "SKILL.md").exists()


def test_omo_skill_copies_repo_skill_and_updates_stale_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    destination = tmp_path / ".agents" / "skills" / "groken" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    _ = destination.write_text("old groken skill")
    result = INSTALLERS["omo-skill"][1](False)
    assert "installed" in result
    assert destination.read_bytes() == Path("skill/SKILL.md").read_bytes()
    assert destination.with_suffix(".md.groken-bak").read_text() == "old groken skill"


def test_new_harness_uninstall_is_safe_when_not_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    for name in ("gjc", "openclaw", "omo-skill"):
        result = uninstall_all(False, only=[name])[name]
        assert "not present" in result or "skipped" in result
    assert list(tmp_path.rglob("*")) == []


def test_gjc_and_openclaw_fallback_without_verified_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    (tmp_path / ".gjc").mkdir()
    (tmp_path / ".openclaw").mkdir()
    assert "no supported config" in INSTALLERS["gjc"][1](False)
    assert "unverifiable" in INSTALLERS["openclaw"][1](False)
    assert list(tmp_path.rglob("*")) == [tmp_path / ".gjc", tmp_path / ".openclaw"]


def test_gjc_verified_user_mcp_config_merges_and_backs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    config = tmp_path / ".gjc" / "agent" / "mcp.json"
    config.parent.mkdir(parents=True)
    _ = config.write_text(json.dumps({"mcpServers": {"other": {"command": "keep"}}}))
    result = INSTALLERS["gjc"][1](False)
    data = _load_json_object(config)
    servers = _require_object_dict(data["mcpServers"])
    assert "installed" in result
    assert "other" in servers and "groken" in servers
    assert config.with_suffix(".json.groken-bak").exists()


def test_gjc_malformed_verified_config_is_skipped_without_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("groken.installers.HOME", tmp_path)
    config = tmp_path / ".gjc" / "agent" / "mcp.json"
    config.parent.mkdir(parents=True)
    original = "{ malformed"
    _ = config.write_text(original)
    result = INSTALLERS["gjc"][1](False)
    assert "unparseable" in result
    assert config.read_text() == original
    assert not config.with_suffix(".json.groken-bak").exists()
