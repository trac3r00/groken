"""Offline inspection of the Grok Bot desktop bundle.

Reads the Electron ``app.asar`` archive with nothing but the standard library
(``struct``/``json``), locates the host bundle, extracts the gateway dispatch
table plus the ``agent.v1.*`` service/method tables by regex, and diffs the
result against the command inventory declared in :mod:`groken.capabilities`.

No shelling out: no ``npx``, no ``asar`` CLI, no Electron tooling.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .capabilities import GATEWAY_COMMANDS

DEFAULT_APP_PATH = Path("/Applications/Grok Bot.app")
HOST_MAIN = "dist/host/host-main.cjs"
PACKAGE_JSON = "package.json"

_ASAR_MAGIC = 4
_HEADER_WORDS = 16
_MAX_HEADER_BYTES = 64 * 1024 * 1024
_RENAME_THRESHOLD = 0.8

# `name:(t,e)=>t.name(Tt(e))` / `name:t=>t.name()` — the minified gateway
# dispatch table maps every wire command onto a same-named host method.
_DISPATCH_ENTRY = re.compile(
    r"([A-Za-z_$][A-Za-z0-9_$]*):"
    r"(?:\(\)|\([A-Za-z_$][A-Za-z0-9_$]*(?:,[A-Za-z_$][A-Za-z0-9_$]*)*\)|[A-Za-z_$][A-Za-z0-9_$]*)"
    r"=>[A-Za-z_$][A-Za-z0-9_$]*\.\1\("
)
# Entries of one object literal are separated by a comma (plus optional
# `async `/whitespace), so a small gap keeps unrelated tables apart.
_DISPATCH_GAP = 10

_SERVICE_HEAD = re.compile(r'typeName:"(agent\.v1\.[A-Za-z0-9_]+)",methods:\{')
_METHOD_NAME = re.compile(r'name:"([A-Za-z0-9_]+)"')
_MAX_SERVICE_BLOCK = 200_000


class AsarError(Exception):
    """The bundle is missing, malformed, or does not contain what we need."""


@dataclass(frozen=True)
class AsarHeader:
    files: dict[str, Any]
    data_offset: int


def _read_exact(path: Path, offset: int, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            blob = handle.read(size)
    except OSError as exc:
        raise AsarError(f"cannot read {path}: {exc}") from exc
    if len(blob) != size:
        raise AsarError(f"truncated archive {path}: wanted {size} bytes at {offset}, got {len(blob)}")
    return blob


def read_asar_header(archive: Path) -> AsarHeader:
    """Parse the pickle-framed JSON index at the head of an asar archive."""
    head = _read_exact(archive, 0, _HEADER_WORDS)
    magic, header_size, str_size, json_len = struct.unpack("<4I", head)
    if magic != _ASAR_MAGIC:
        raise AsarError(f"bad asar magic in {archive}: {magic}")
    if header_size > _MAX_HEADER_BYTES or str_size + 4 != header_size:
        raise AsarError(f"bad asar header framing in {archive}: {header_size}/{str_size}")
    if json_len == 0 or json_len > str_size - 4:
        raise AsarError(f"bad asar header length in {archive}: {json_len}")

    payload = _read_exact(archive, _HEADER_WORDS, json_len)
    try:
        index = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise AsarError(f"unreadable asar index in {archive}: {exc}") from exc
    if not isinstance(index, dict) or not isinstance(index.get("files"), dict):
        raise AsarError(f"asar index in {archive} has no file tree")
    return AsarHeader(files=index["files"], data_offset=8 + header_size)


def _lookup(header: AsarHeader, entry: str) -> dict[str, Any]:
    node: dict[str, Any] = {"files": header.files}
    for part in entry.split("/"):
        children = node.get("files")
        if not isinstance(children, dict) or part not in children:
            raise AsarError(f"entry not found in archive: {entry}")
        child = children[part]
        if not isinstance(child, dict):
            raise AsarError(f"malformed index node for {entry}")
        node = child
    if "offset" not in node or "size" not in node:
        raise AsarError(f"{entry} is not a regular file in the archive")
    return node


def read_asar_entry(archive: Path, header: AsarHeader, entry: str) -> bytes:
    """Return the raw bytes of ``entry`` (POSIX-ish path inside the archive)."""
    node = _lookup(header, entry)
    try:
        offset = int(node["offset"])
        size = int(node["size"])
    except (TypeError, ValueError) as exc:
        raise AsarError(f"bad offset/size for {entry}: {exc}") from exc
    if offset < 0 or size < 0:
        raise AsarError(f"bad offset/size for {entry}: {offset}/{size}")
    return _read_exact(archive, header.data_offset + offset, size)


def resolve_asar(app_path: Path) -> Path:
    """Locate app.asar given a .app bundle, a resources dir, or the file itself."""
    candidates = (
        app_path,
        app_path / "Contents" / "Resources" / "app.asar",
        app_path / "app.asar",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AsarError(f"no app.asar found under {app_path}")


def extract_command_names(source: str) -> list[str]:
    """Return the largest contiguous gateway dispatch table found in ``source``."""
    best: list[str] = []
    current: list[str] = []
    previous_end: int | None = None
    for match in _DISPATCH_ENTRY.finditer(source):
        if previous_end is not None and match.start() - previous_end <= _DISPATCH_GAP:
            current.append(match.group(1))
        else:
            if len(current) > len(best):
                best = current
            current = [match.group(1)]
        previous_end = match.end()
    if len(current) > len(best):
        best = current
    return sorted(set(best))


def extract_service_methods(source: str) -> dict[str, list[str]]:
    """Map ``agent.v1.*`` service type names to their declared RPC method names."""
    services: dict[str, list[str]] = {}
    for head in _SERVICE_HEAD.finditer(source):
        start = head.end()
        depth = 1
        index = start
        limit = min(len(source), start + _MAX_SERVICE_BLOCK)
        while index < limit and depth:
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        if depth:
            continue
        block = source[start : index - 1]
        methods = sorted(set(_METHOD_NAME.findall(block)))
        if methods:
            services[head.group(1)] = methods
    return services


def diff_commands(*, found: list[str], expected: list[str]) -> dict[str, Any]:
    """Diff a discovered command list against the expected inventory."""
    found_set, expected_set = set(found), set(expected)
    added = sorted(found_set - expected_set)
    removed = sorted(expected_set - found_set)

    pairs: list[tuple[float, str, str]] = []
    for gone in removed:
        for new in added:
            ratio = SequenceMatcher(None, gone, new).ratio()
            if ratio >= _RENAME_THRESHOLD:
                pairs.append((ratio, gone, new))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))

    renamed: list[dict[str, str]] = []
    used_old: set[str] = set()
    used_new: set[str] = set()
    for _, gone, new in pairs:
        if gone in used_old or new in used_new:
            continue
        used_old.add(gone)
        used_new.add(new)
        renamed.append({"from": gone, "to": new})

    added = [name for name in added if name not in used_new]
    removed = [name for name in removed if name not in used_old]
    return {
        "added": added,
        "removed": removed,
        "renamed": sorted(renamed, key=lambda item: item["from"]),
        "clean": not (added or removed or renamed),
    }


def _app_version(archive: Path, header: AsarHeader) -> str | None:
    try:
        meta = json.loads(read_asar_entry(archive, header, PACKAGE_JSON))
    except (AsarError, ValueError):
        return None
    version = meta.get("version") if isinstance(meta, dict) else None
    return version if isinstance(version, str) else None


def inspect_app(app_path: Path | str | None = None) -> dict[str, Any]:
    """Parse the bundle at ``app_path`` and diff it against capabilities.py."""
    root = Path(app_path) if app_path is not None else DEFAULT_APP_PATH
    archive = resolve_asar(root)
    header = read_asar_header(archive)
    source = read_asar_entry(archive, header, HOST_MAIN).decode("utf-8", "replace")

    commands = extract_command_names(source)
    if not commands:
        raise AsarError(f"no gateway dispatch table found in {HOST_MAIN}")
    expected = [spec.name for spec in GATEWAY_COMMANDS]
    return {
        "app_path": str(root),
        "asar": str(archive),
        "app_version": _app_version(archive, header),
        "host_main": HOST_MAIN,
        "host_main_bytes": len(source),
        "command_count": len(commands),
        "expected_count": len(expected),
        "commands": commands,
        "services": extract_service_methods(source),
        "drift": diff_commands(found=commands, expected=expected),
    }
