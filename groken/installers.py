import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from typing_extensions import override

HOME = Path.home()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "groken"
CLI_TOOL_SPEC = ".[mcp,share,worker]"
MCP_INSTALL_COMMAND: Final = "uv tool install --force 'groken[mcp]'"

JsonObject = dict[str, object]
Installer = tuple[Callable[[], bool], Callable[[bool], str]]


# Exception instances must remain mutable so Python can attach traceback state.
@dataclass(slots=True)
class McpDependencyError(Exception):
    install_command: str = MCP_INSTALL_COMMAND

    @override
    def __str__(self) -> str:
        return (
            "MCP support is not installed; install it with: "
            f"{self.install_command}"
        )


def _object_dict(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    result: JsonObject = {}
    for key, item in cast("dict[object, object]", value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def install_cli_command(dry_run: bool) -> str:
    bin_dir = Path(os.environ.get("UV_TOOL_BIN_DIR", HOME / ".local" / "bin"))
    command_path = bin_dir.expanduser() / "groken"
    if command_path.exists():
        return f"already present -> {command_path}"
    if (PROJECT_ROOT / "pyproject.toml").exists():
        if dry_run:
            return f"would install -> {command_path}"
        uv = shutil.which("uv")
        if uv is None:
            return "failed (uv is not installed)"
        command = [uv, "tool", "install", "--editable", CLI_TOOL_SPEC]
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return f"failed (uv tool install exited {completed.returncode}): {detail}"
        return f"installed -> {command_path}"
    current_entrypoint = Path(sys.executable).with_name("groken")
    if current_entrypoint.exists():
        return f"already present -> {current_entrypoint}"
    return f"failed (project checkout not found: {PROJECT_ROOT})"


def mcp_command() -> str:
    if importlib.util.find_spec("mcp") is None:
        raise McpDependencyError
    candidate = Path(sys.executable).parent / "groken-mcp"
    if candidate.exists():
        return str(candidate)
    return shutil.which("groken-mcp") or str(candidate)


def skill_source() -> Path:
    checkout_source = PROJECT_ROOT / "skill" / "SKILL.md"
    if checkout_source.exists():
        return checkout_source
    return Path(sys.prefix) / "skill" / "SKILL.md"


def _backup(path: Path) -> None:
    if path.exists():
        _ = shutil.copy2(path, path.with_suffix(path.suffix + ".groken-bak"))


def _jsonc_mask_comments(text: str) -> str:
    masked = list(text)
    i = 0
    string = False
    while i < len(text):
        if string:
            if text[i] == "\\" and i + 1 < len(text):
                i += 2
                continue
            if text[i] == '"':
                string = False
            i += 1
            continue
        if text[i] == '"':
            string = True
            i += 1
            continue
        end = i
        if text.startswith("//", i):
            newline = text.find("\n", i)
            end = len(text) if newline < 0 else newline
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            end = len(text) if close < 0 else close + 2
        if end > i:
            masked[i:end] = " " * (end - i)
            i = end
            continue
        i += 1
    return "".join(masked)


def _jsonc_clean(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", _jsonc_mask_comments(text))


def _jsonc_object_span(text: str, key: str) -> tuple[int, int] | None:
    masked = _jsonc_mask_comments(text)
    match = re.search(r'"' + re.escape(key) + r'"\s*:', masked)
    if not match:
        return None
    start = masked.find("{", match.end())
    if start < 0:
        return None
    depth = 0
    string = False
    i = start
    while i < len(masked):
        c = masked[i]
        if string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                string = False
        elif c == '"':
            string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    return None


def _jsonc_value_end(text: str, start: int) -> int:
    masked = _jsonc_mask_comments(text)
    depth = 0
    string = False
    i = start
    while i < len(masked):
        c = masked[i]
        if string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                string = False
        elif c == '"':
            string = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            if depth == 0:
                return i
            depth -= 1
        elif c == "," and depth == 0:
            return i
        i += 1
    return i


def install_json_mcp(
    path: Path,
    key: str,
    command: str,
    dry_run: bool,
    entry: JsonObject | None = None,
    jsonc: bool = False,
) -> str:
    payload = entry or {"type": "stdio", "command": command, "args": []}
    if dry_run:
        return f"would write {key}.{SERVER_NAME} -> {path}"
    data: JsonObject = {}
    if path.exists():
        text = path.read_text()
        try:
            loaded = cast(
                "object", json.loads(_jsonc_clean(text) if jsonc else text or "{}")
            )
        except ValueError:
            return f"skipped (unparseable JSON): {path}"
        parsed = _object_dict(loaded)
        if parsed is None:
            raise TypeError(f"JSON root is not an object: {path}")
        data = parsed
        if jsonc:
            span = _jsonc_object_span(text, key)
            if span and re.search(
                r'"groken"\s*:', _jsonc_mask_comments(text[span[0] : span[1]])
            ):
                return f"already present -> {path}"
            if span:
                a, b = span
                body = text[a + 1 : b]
                indent_match = re.search(r"\n([ \t]*)[^\n]*$", text[:b])
                indent = indent_match.group(1) if indent_match is not None else "  "
                has_comma = bool(
                    re.search(r",\s*$", _jsonc_mask_comments(body))
                )
                addition = (
                    "," if body.strip() and not has_comma else ""
                ) + f'\n{indent}  "{SERVER_NAME}": {json.dumps(payload)}\n{indent}'
                _backup(path)
                _ = path.write_text(text[:b] + addition + text[b:])
                return f"installed -> {path}"
        _backup(path)
    section = _object_dict(data.get(key))
    if section is None:
        section = {}
    section[SERVER_NAME] = payload
    data[key] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps(data, indent=2) + "\n")
    return f"installed -> {path}"


def _toml_mask_strings_and_comments(text: str) -> str:
    masked = list(text)

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if text[index] != "\n":
                masked[index] = " "

    i = 0
    quote: str | None = None
    while i < len(text):
        if quote is None:
            if text.startswith(('"""', "'''"), i):
                quote = text[i : i + 3]
                blank(i, i + 3)
                i += 3
                continue
            if text[i] in {'"', "'"}:
                quote = text[i]
                blank(i, i + 1)
                i += 1
                continue
            if text[i] == "#":
                end = text.find("\n", i)
                end = len(text) if end < 0 else end
                blank(i, end)
                i = end
                continue
            i += 1
            continue
        if len(quote) == 3 and text.startswith(quote, i):
            blank(i, i + 3)
            quote = None
            i += 3
            continue
        if len(quote) == 1 and text[i] == quote:
            blank(i, i + 1)
            quote = None
            i += 1
            continue
        if quote in {'"', '"""'} and text[i] == "\\" and i + 1 < len(text):
            blank(i, i + 2)
            i += 2
            continue
        blank(i, i + 1)
        i += 1
    return "".join(masked)


def _toml_section_span(text: str, header: str) -> tuple[int, int] | None:
    masked = _toml_mask_strings_and_comments(text)
    current = re.search(
        rf"(?m)^[ \t]*{re.escape(header)}[ \t]*$",
        masked,
    )
    if current is None:
        return None
    following = re.search(
        r"(?m)^[ \t]*\[\[?[^\n]+\]\]?[ \t]*$",
        masked[current.end() :],
    )
    end = len(text) if following is None else current.end() + following.start()
    return current.start(), end


def install_toml_mcp(path: Path, command: str, dry_run: bool) -> str:
    header = f"[mcp_servers.{SERVER_NAME}]"
    block = f"{header}\ncommand = {json.dumps(command)}\nargs = []\n"
    if dry_run:
        return f"would write {header} -> {path}"
    text = path.read_text() if path.exists() else ""
    try:
        _ = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return f"skipped (unparseable TOML): {path}"
    span = _toml_section_span(text, header)
    if span is not None:
        start, end = span
        text = text[:start] + block + text[end:]
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    if path.exists():
        _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text)
    return f"installed -> {path}"


def install_yaml_mcp_hermes(path: Path, command: str, dry_run: bool) -> str:
    entry = (
        f"  {SERVER_NAME}:\n"
        f"    command: {json.dumps(command)}\n"
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
    _ = path.write_text(text)
    return f"installed -> {path}"


def install_skill_dir(dest_root: Path, source: Path, dry_run: bool) -> str:
    dest = dest_root / SERVER_NAME / "SKILL.md"
    if not source.exists():
        return f"failed (no skill source at {source})"
    if dry_run:
        return f"would copy skill -> {dest}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _backup(dest)
    _ = shutil.copy2(source, dest)
    return f"installed -> {dest}"


def _install_cursor_skills(dry_run: bool) -> str:
    source = skill_source()
    canonical = install_skill_dir(HOME / ".cursor" / "skills", source, dry_run)
    legacy_root = HOME / ".cursor" / "skills-cursor"
    if not legacy_root.exists():
        return canonical
    legacy = install_skill_dir(legacy_root, source, dry_run)
    return f"{canonical}; legacy mirror: {legacy}"


def _uninstall_cursor_skills(dry_run: bool) -> str:
    canonical = uninstall_skill_dir(HOME / ".cursor" / "skills", dry_run)
    legacy_root = HOME / ".cursor" / "skills-cursor"
    if not legacy_root.exists():
        return canonical
    legacy = uninstall_skill_dir(legacy_root, dry_run)
    return f"{canonical}; legacy mirror: {legacy}"


def _install_gjc(dry_run: bool) -> str:
    path = HOME / ".gjc" / "agent" / "mcp.json"
    if not path.exists():
        return f"skipped (no supported config; use CLI shelling per skill/SKILL.md): {path}"
    return install_json_mcp(path, "mcpServers", mcp_command(), dry_run)


def _install_openclaw(_dry_run: bool) -> str:
    return "skipped (unverifiable config schema; no changes)"


def _uninstall_gjc(dry_run: bool) -> str:
    return uninstall_json_mcp(
        HOME / ".gjc" / "agent" / "mcp.json", "mcpServers", dry_run
    )


def _uninstall_openclaw(_dry_run: bool) -> str:
    return "not present (no verified config schema)"


def _json_target(path: Path, key: str, entry: JsonObject | None = None) -> Installer:
    return (
        lambda: path.parent.exists(),
        lambda dry_run: install_json_mcp(path, key, mcp_command(), dry_run, entry),
    )


def _opencode_entry() -> JsonObject:
    return {"type": "local", "command": [mcp_command()], "enabled": True}


def _opencode_config() -> Path:
    root = HOME / ".config" / "opencode"
    jsonc = root / "opencode.jsonc"
    return jsonc if jsonc.exists() else root / "opencode.json"


def _install_opencode(dry_run: bool) -> str:
    path = _opencode_config()
    return install_json_mcp(
        path,
        "mcp",
        mcp_command(),
        dry_run,
        _opencode_entry(),
        jsonc=path.suffix == ".jsonc",
    )


def _uninstall_opencode(dry_run: bool) -> str:
    path = _opencode_config()
    return uninstall_json_mcp(path, "mcp", dry_run, jsonc=path.suffix == ".jsonc")


INSTALLERS: dict[str, Installer] = {
    "claude-code": _json_target(HOME / ".claude.json", "mcpServers"),
    "claude-desktop": _json_target(
        HOME
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json",
        "mcpServers",
    ),
    "cursor": _json_target(HOME / ".cursor" / "mcp.json", "mcpServers"),
    "windsurf": _json_target(
        HOME / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers"
    ),
    "gemini-cli": _json_target(HOME / ".gemini" / "settings.json", "mcpServers"),
    "vscode": _json_target(
        HOME / "Library" / "Application Support" / "Code" / "User" / "mcp.json",
        "servers",
    ),
    "kiro": _json_target(HOME / ".kiro" / "settings" / "mcp.json", "mcpServers"),
    "opencode": (
        lambda: (HOME / ".config" / "opencode").exists(),
        _install_opencode,
    ),
    "codex": (
        lambda: (HOME / ".codex").exists(),
        lambda dry_run: install_toml_mcp(
            HOME / ".codex" / "config.toml", mcp_command(), dry_run
        ),
    ),
    "hermes": (
        lambda: (HOME / ".hermes" / "config.yaml").exists(),
        lambda dry_run: install_yaml_mcp_hermes(
            HOME / ".hermes" / "config.yaml", mcp_command(), dry_run
        ),
    ),
    "omo": (
        lambda: (HOME / ".agents").exists(),
        lambda dry_run: install_skill_dir(
            HOME / ".agents" / "skills", skill_source(), dry_run
        ),
    ),
    "claude-skills": (
        lambda: (HOME / ".claude").exists(),
        lambda dry_run: install_skill_dir(
            HOME / ".claude" / "skills", skill_source(), dry_run
        ),
    ),
    "codex-skills": (
        lambda: (HOME / ".codex").exists(),
        lambda dry_run: install_skill_dir(
            HOME / ".codex" / "skills", skill_source(), dry_run
        ),
    ),
    "cursor-skills": (
        lambda: (HOME / ".cursor").exists(),
        _install_cursor_skills,
    ),
    "gjc": (
        lambda: (HOME / ".local" / "bin" / "gjc").exists() or (HOME / ".gjc").exists(),
        _install_gjc,
    ),
    "gjc-skills": (
        lambda: (HOME / ".gjc").exists(),
        lambda dry_run: install_skill_dir(
            HOME / ".gjc" / "agent" / "skills", skill_source(), dry_run
        ),
    ),
    "openclaw": (
        lambda: (
            (HOME / ".openclaw").exists() or (HOME / ".config" / "openclaw").exists()
        ),
        _install_openclaw,
    ),
    "omo-skill": (
        lambda: (HOME / ".agents").exists(),
        lambda dry_run: install_skill_dir(
            HOME / ".agents" / "skills", skill_source(), dry_run
        ),
    ),
}


def uninstall_json_mcp(path: Path, key: str, dry_run: bool, jsonc: bool = False) -> str:
    if not path.exists():
        return f"not present (no {path})"
    text = path.read_text()
    try:
        loaded = cast(
            "object", json.loads(_jsonc_clean(text) if jsonc else text or "{}")
        )
    except ValueError:
        return f"skipped (unparseable JSON): {path}"
    data = _object_dict(loaded)
    if data is None:
        raise TypeError(f"JSON root is not an object: {path}")
    section = _object_dict(data.get(key))
    if section is None or SERVER_NAME not in section:
        return f"not present -> {path}"
    if dry_run:
        return f"would remove {key}.{SERVER_NAME} <- {path}"
    if jsonc:
        span = _jsonc_object_span(text, key)
        if span:
            section_text = text[span[0] : span[1]]
            match = re.search(r'"groken"\s*:', section_text)
            if match:
                begin = span[0] + match.start()
                value = text.find(":", begin) + 1
                end = _jsonc_value_end(text, value)
                if text[end : end + 1] == ",":
                    end += 1
                _backup(path)
                _ = path.write_text(text[:begin] + text[end:])
                return f"removed <- {path}"
    _backup(path)
    del section[SERVER_NAME]
    data[key] = section
    _ = path.write_text(json.dumps(data, indent=2) + "\n")
    return f"removed <- {path}"


def uninstall_toml_mcp(path: Path, dry_run: bool) -> str:
    header = f"[mcp_servers.{SERVER_NAME}]"
    if not path.exists():
        return f"not present (no {path})"
    text = path.read_text()
    try:
        _ = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return f"skipped (unparseable TOML): {path}"
    span = _toml_section_span(text, header)
    if span is None:
        return f"not present -> {path}"
    if dry_run:
        return f"would remove {header} <- {path}"
    start, end = span
    _backup(path)
    _ = path.write_text((text[:start] + text[end:]).rstrip("\n") + "\n")
    return f"removed <- {path}"


def uninstall_skill_dir(dest_root: Path, dry_run: bool) -> str:
    dest = dest_root / SERVER_NAME
    if not dest.exists():
        return f"not present -> {dest}"
    if dry_run:
        return f"would remove skill <- {dest}"
    shutil.rmtree(dest)
    return f"removed <- {dest}"


def install_all(dry_run: bool, only: list[str] | None = None) -> dict[str, str]:
    names = list(only) if only is not None else list(INSTALLERS)
    if not names:
        raise ValueError("no agents selected")
    unknown = [n for n in names if n not in INSTALLERS]
    if unknown:
        raise ValueError(
            f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(INSTALLERS)}"
        )
    results: dict[str, str] = {}
    for name in names:
        detect, run = INSTALLERS[name]
        try:
            results[name] = (
                run(dry_run)
                if only is not None or detect()
                else "skipped (not installed)"
            )
        except McpDependencyError as exc:
            results[name] = f"failed ({exc})"
    return results


def detected_agents() -> list[str]:
    return [name for name, (detect, _) in INSTALLERS.items() if detect()]


UNINSTALLERS: dict[str, Callable[[bool], str]] = {
    "claude-code": lambda dry_run: uninstall_json_mcp(
        HOME / ".claude.json", "mcpServers", dry_run
    ),
    "claude-desktop": lambda dry_run: uninstall_json_mcp(
        HOME
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json",
        "mcpServers",
        dry_run,
    ),
    "cursor": lambda dry_run: uninstall_json_mcp(
        HOME / ".cursor" / "mcp.json", "mcpServers", dry_run
    ),
    "windsurf": lambda dry_run: uninstall_json_mcp(
        HOME / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers", dry_run
    ),
    "gemini-cli": lambda dry_run: uninstall_json_mcp(
        HOME / ".gemini" / "settings.json", "mcpServers", dry_run
    ),
    "vscode": lambda dry_run: uninstall_json_mcp(
        HOME / "Library" / "Application Support" / "Code" / "User" / "mcp.json",
        "servers",
        dry_run,
    ),
    "kiro": lambda dry_run: uninstall_json_mcp(
        HOME / ".kiro" / "settings" / "mcp.json", "mcpServers", dry_run
    ),
    "opencode": _uninstall_opencode,
    "codex": lambda dry_run: uninstall_toml_mcp(
        HOME / ".codex" / "config.toml", dry_run
    ),
    "omo": lambda dry_run: uninstall_skill_dir(HOME / ".agents" / "skills", dry_run),
    "claude-skills": lambda dry_run: uninstall_skill_dir(
        HOME / ".claude" / "skills", dry_run
    ),
    "codex-skills": lambda dry_run: uninstall_skill_dir(
        HOME / ".codex" / "skills", dry_run
    ),
    "cursor-skills": _uninstall_cursor_skills,
    "gjc": _uninstall_gjc,
    "gjc-skills": lambda dry_run: uninstall_skill_dir(
        HOME / ".gjc" / "agent" / "skills", dry_run
    ),
    "openclaw": _uninstall_openclaw,
    "omo-skill": lambda dry_run: uninstall_skill_dir(
        HOME / ".agents" / "skills", dry_run
    ),
}


def uninstall_all(dry_run: bool, only: list[str] | None = None) -> dict[str, str]:
    names = list(only) if only is not None else list(UNINSTALLERS)
    if not names:
        raise ValueError("no agents selected")
    unknown = [n for n in names if n not in UNINSTALLERS]
    if unknown:
        raise ValueError(
            f"unknown or non-removable agent(s): {', '.join(unknown)}; known: {', '.join(UNINSTALLERS)}"
        )
    return {name: UNINSTALLERS[name](dry_run) for name in names}
