import json

import pytest

from groken.installers import (
    INSTALLERS,
    install_all,
    install_json_mcp,
    install_skill_dir,
    install_toml_mcp,
)


def test_json_mcp_merge_is_idempotent_and_preserves_siblings(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "unrelated": 1}))
    for _ in range(2):
        install_json_mcp(cfg, key="mcpServers", command="/bin/groken-mcp", dry_run=False)
    data = json.loads(cfg.read_text())
    assert data["unrelated"] == 1
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["groken"]["command"] == "/bin/groken-mcp"
    assert len(data["mcpServers"]) == 2


def test_json_mcp_creates_file_when_absent(tmp_path):
    cfg = tmp_path / "nested" / "mcp.json"
    install_json_mcp(cfg, key="servers", command="/bin/groken-mcp", dry_run=False)
    assert json.loads(cfg.read_text())["servers"]["groken"]["command"] == "/bin/groken-mcp"


def test_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / "mcp.json"
    install_json_mcp(cfg, key="mcpServers", command="/bin/groken-mcp", dry_run=True)
    assert not cfg.exists()


def test_opencode_shape_uses_command_array(tmp_path):
    cfg = tmp_path / "opencode.json"
    install_json_mcp(cfg, key="mcp", command="/bin/groken-mcp", dry_run=False,
                     entry={"type": "local", "command": ["/bin/groken-mcp"], "enabled": True})
    entry = json.loads(cfg.read_text())["mcp"]["groken"]
    assert entry["command"] == ["/bin/groken-mcp"]
    assert entry["enabled"] is True


def test_toml_mcp_merge_is_idempotent_and_preserves_siblings(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt"\n\n[mcp_servers.other]\ncommand = "x"\n')
    for _ in range(2):
        install_toml_mcp(cfg, command="/bin/groken-mcp", dry_run=False)
    text = cfg.read_text()
    assert text.count("[mcp_servers.groken]") == 1
    assert "[mcp_servers.other]" in text
    assert 'model = "gpt"' in text
    assert '/bin/groken-mcp' in text


def test_skill_dir_install_copies_skill(tmp_path):
    src = tmp_path / "SKILL.md"
    src.write_text("---\nname: groken\n---\nbody")
    dest_root = tmp_path / "skills"
    install_skill_dir(dest_root, src, dry_run=False)
    assert (dest_root / "groken" / "SKILL.md").read_text().startswith("---")


def test_install_all_skips_missing_targets(tmp_path, monkeypatch):
    seen = []

    def fake_installer(dry_run):
        seen.append("ran")
        return "installed"

    monkeypatch.setitem(INSTALLERS, "fake-present", (lambda: True, fake_installer))
    monkeypatch.setitem(INSTALLERS, "fake-absent", (lambda: False, fake_installer))
    results = install_all(dry_run=False, only=["fake-present", "fake-absent"])
    assert results["fake-present"] == "installed"
    assert results["fake-absent"] == "skipped (not installed)"
    assert seen == ["ran"]


def test_install_all_rejects_unknown_agent():
    with pytest.raises(ValueError):
        install_all(dry_run=True, only=["nope-not-real"])
