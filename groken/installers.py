import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOME = Path.home()
SERVER_NAME = "groken"


def mcp_command() -> str:
    candidate = Path(sys.executable).parent / "groken-mcp"
    if candidate.exists():
        return str(candidate)
    return shutil.which("groken-mcp") or str(candidate)


def skill_source() -> Path:
    return Path(__file__).resolve().parent.parent / "skill" / "SKILL.md"


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".groken-bak"))


def install_json_mcp(
    path: Path,
    key: str,
    command: str,
    dry_run: bool,
    entry: dict[str, Any] | None = None,
) -> str:
    payload = entry or {"type": "stdio", "command": command, "args": []}
    if dry_run:
        return f"would write {key}.{SERVER_NAME} -> {path}"
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except ValueError:
            return f"skipped (unparseable JSON): {path}"
        _backup(path)
    section = data.get(key)
    if not isinstance(section, dict):
        section = {}
    section[SERVER_NAME] = payload
    data[key] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return f"installed -> {path}"


def install_toml_mcp(path: Path, command: str, dry_run: bool) -> str:
    header = f"[mcp_servers.{SERVER_NAME}]"
    block = f'{header}\ncommand = "{command}"\nargs = []\n'
    if dry_run:
        return f"would write {header} -> {path}"
    text = path.read_text() if path.exists() else ""
    if header in text:
        start = text.index(header)
        rest = text[start + len(header):]
        next_section = rest.find("\n[")
        end = len(text) if next_section == -1 else start + len(header) + next_section + 1
        text = text[:start] + block + text[end:]
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    if path.exists():
        _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return f"installed -> {path}"


def install_yaml_mcp_hermes(path: Path, command: str, dry_run: bool) -> str:
    entry = (
        f"  {SERVER_NAME}:\n"
        f"    command: {command}\n"
        f"    enabled: true\n"
        f"    connect_timeout: 30\n"
        f"    timeout: 660\n"
    )
    if dry_run:
        return f"would ensure mcp_servers.{SERVER_NAME} -> {path}"
    if not path.exists():
        return f"skipped (missing): {path}"
    text = path.read_text()
    if f"\n  {SERVER_NAME}:\n" in text:
        return f"already present -> {path}"
    if "mcp_servers:\n" not in text:
        return f"skipped (no mcp_servers section): {path}"
    _backup(path)
    text = text.replace("mcp_servers:\n", "mcp_servers:\n" + entry, 1)
    path.write_text(text)
    return f"installed -> {path}"


def install_skill_dir(dest_root: Path, source: Path, dry_run: bool) -> str:
    dest = dest_root / SERVER_NAME / "SKILL.md"
    if dry_run:
        return f"would copy skill -> {dest}"
    if not source.exists():
        return f"skipped (no skill source at {source})"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return f"installed -> {dest}"


def _json_target(path: Path, key: str, entry: dict[str, Any] | None = None):
    return (
        lambda: path.parent.exists(),
        lambda dry_run: install_json_mcp(path, key, mcp_command(), dry_run, entry),
    )


def _opencode_entry() -> dict[str, Any]:
    return {"type": "local", "command": [mcp_command()], "enabled": True}


INSTALLERS: dict[str, tuple[Callable[[], bool], Callable[[bool], str]]] = {
    "claude-code": _json_target(HOME / ".claude.json", "mcpServers"),
    "claude-desktop": _json_target(
        HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json", "mcpServers"
    ),
    "cursor": _json_target(HOME / ".cursor" / "mcp.json", "mcpServers"),
    "windsurf": _json_target(HOME / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers"),
    "gemini-cli": _json_target(HOME / ".gemini" / "settings.json", "mcpServers"),
    "vscode": _json_target(
        HOME / "Library" / "Application Support" / "Code" / "User" / "mcp.json", "servers"
    ),
    "kiro": _json_target(HOME / ".kiro" / "settings" / "mcp.json", "mcpServers"),
    "opencode": (
        lambda: (HOME / ".config" / "opencode").exists(),
        lambda dry_run: install_json_mcp(
            HOME / ".config" / "opencode" / "opencode.json", "mcp", mcp_command(), dry_run, _opencode_entry()
        ),
    ),
    "codex": (
        lambda: (HOME / ".codex").exists(),
        lambda dry_run: install_toml_mcp(HOME / ".codex" / "config.toml", mcp_command(), dry_run),
    ),
    "hermes": (
        lambda: (HOME / ".hermes" / "config.yaml").exists(),
        lambda dry_run: install_yaml_mcp_hermes(HOME / ".hermes" / "config.yaml", mcp_command(), dry_run),
    ),
    "omo": (
        lambda: (HOME / ".agents" / "skills").exists(),
        lambda dry_run: install_skill_dir(HOME / ".agents" / "skills", skill_source(), dry_run),
    ),
    "claude-skills": (
        lambda: (HOME / ".claude" / "skills").exists(),
        lambda dry_run: install_skill_dir(HOME / ".claude" / "skills", skill_source(), dry_run),
    ),
}


def install_all(dry_run: bool, only: list[str] | None = None) -> dict[str, str]:
    names = only or list(INSTALLERS)
    unknown = [n for n in names if n not in INSTALLERS]
    if unknown:
        raise ValueError(f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(INSTALLERS)}")
    results: dict[str, str] = {}
    for name in names:
        detect, run = INSTALLERS[name]
        results[name] = run(dry_run) if detect() else "skipped (not installed)"
    return results
