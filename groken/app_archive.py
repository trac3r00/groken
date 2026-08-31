"""Minimal standard-library reader for Electron ASAR application metadata."""

from __future__ import annotations

import hashlib
import json
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast
from xml.parsers.expat import ExpatError

from .inspect_contracts import CommandContract, extract_command_contracts

DEFAULT_APP_PATH = Path("/Applications/Grok Bot.app")
HOST_MAIN = "dist/host/host-main.cjs"
COORDINATOR_MAIN = "dist/node-agent-coordinator/main.cjs"
PACKAGE_JSON = "package.json"
_ASAR_MAGIC = 4
_HEADER_WORDS = 16
_MAX_HEADER_BYTES = 64 * 1024 * 1024

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class AsarError(Exception):
    """The bundle is missing, malformed, or lacks a known gateway table."""


@dataclass(frozen=True, slots=True)
class AsarHeader:
    files: JsonObject
    data_offset: int


def _object_dict(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    return value


def _read_exact(path: Path, offset: int, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            _ = handle.seek(offset)
            blob = handle.read(size)
    except OSError as exc:
        raise AsarError(f"cannot read {path}: {exc}") from exc
    if len(blob) != size:
        raise AsarError(
            f"truncated archive {path}: wanted {size} bytes at {offset}, got {len(blob)}"
        )
    return blob


def read_asar_header(archive: Path) -> AsarHeader:
    """Parse the pickle-framed JSON index at the head of an ASAR archive."""
    head = _read_exact(archive, 0, _HEADER_WORDS)
    magic, header_size, str_size, json_len = (
        int.from_bytes(head[offset : offset + 4], "little")
        for offset in range(0, _HEADER_WORDS, 4)
    )
    if magic != _ASAR_MAGIC:
        raise AsarError(f"bad asar magic in {archive}: {magic}")
    if header_size > _MAX_HEADER_BYTES or str_size + 4 != header_size:
        raise AsarError(
            f"bad asar header framing in {archive}: {header_size}/{str_size}"
        )
    if json_len == 0 or json_len > str_size - 4:
        raise AsarError(f"bad asar header length in {archive}: {json_len}")
    payload = _read_exact(archive, _HEADER_WORDS, json_len)
    try:
        loaded = cast("JsonValue", json.loads(payload))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AsarError(f"unreadable asar index in {archive}: {exc}") from exc
    index = _object_dict(loaded)
    files = _object_dict(index.get("files")) if index is not None else None
    if files is None:
        raise AsarError(f"asar index in {archive} has no file tree")
    return AsarHeader(files=files, data_offset=8 + header_size)


def _lookup(header: AsarHeader, entry: str) -> JsonObject:
    node: JsonObject = {"files": header.files}
    for part in entry.split("/"):
        children = _object_dict(node.get("files"))
        if children is None or part not in children:
            raise AsarError(f"entry not found in archive: {entry}")
        child = _object_dict(children[part])
        if child is None:
            raise AsarError(f"malformed index node for {entry}")
        node = child
    if "offset" not in node or "size" not in node:
        raise AsarError(f"{entry} is not a regular file in the archive")
    return node


def read_asar_entry(archive: Path, header: AsarHeader, entry: str) -> bytes:
    """Return the raw bytes of one regular archive entry."""
    node = _lookup(header, entry)
    try:
        raw_offset, raw_size = node["offset"], node["size"]
        if not isinstance(raw_offset, (str, int, float)) or not isinstance(
            raw_size, (str, int, float)
        ):
            raise AsarError(f"bad offset/size for {entry}: values must be numeric")
        offset, size = int(raw_offset), int(raw_size)
    except ValueError as exc:
        raise AsarError(f"bad offset/size for {entry}: {exc}") from exc
    if offset < 0 or size < 0:
        raise AsarError(f"bad offset/size for {entry}: {offset}/{size}")
    return _read_exact(archive, header.data_offset + offset, size)


def resolve_asar(app_path: Path) -> Path:
    """Locate app.asar from a bundle, resources directory, or archive path."""
    for candidate in (
        app_path,
        app_path / "Contents" / "Resources" / "app.asar",
        app_path / "app.asar",
    ):
        if candidate.is_file():
            return candidate
    raise AsarError(f"no app.asar found under {app_path}")


def read_package_metadata(
    archive: Path, header: AsarHeader
) -> tuple[str | None, str | None]:
    try:
        loaded = cast(
            "JsonValue", json.loads(read_asar_entry(archive, header, PACKAGE_JSON))
        )
    except (AsarError, ValueError):
        return None, None
    metadata = _object_dict(loaded)
    if metadata is None:
        return None, None
    version, main = metadata.get("version"), metadata.get("main")
    return (
        version if isinstance(version, str) else None,
        main if isinstance(main, str) else None,
    )


def read_bundle_version(root: Path) -> str | None:
    try:
        loaded = cast(
            "JsonValue",
            plistlib.loads((root / "Contents" / "Info.plist").read_bytes()),
        )
    except (OSError, ValueError, ExpatError):
        return None
    metadata = _object_dict(loaded)
    version = metadata.get("CFBundleShortVersionString") if metadata else None
    return version if isinstance(version, str) and version else None


def read_host_blob(
    archive: Path, header: AsarHeader, package_main: str | None
) -> tuple[str, bytes, tuple[CommandContract, ...]]:
    for entry in (HOST_MAIN, COORDINATOR_MAIN, package_main):
        if entry is None:
            continue
        try:
            blob = read_asar_entry(archive, header, entry)
        except AsarError:
            continue
        contracts = extract_command_contracts(blob.decode("utf-8", "replace"))
        if contracts:
            return entry, blob, contracts
    raise AsarError("no recognized gateway dispatch or validator table found")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AsarError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()
