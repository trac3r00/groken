from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias, TypeGuard

from typing_extensions import override

from .env_collectors import Inventory
from .env_persistence import CurrentManifestError, read_current_manifest

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True, slots=True)
class LoadedInventory:
    manifest_id: str
    path: Path
    inventory: Inventory
    brewfile_path: Path | None


@dataclass(frozen=True, slots=True)
class RestoreManifestError(Exception):
    detail: str

    @override
    def __str__(self) -> str:
        return self.detail


def _record(value: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    return isinstance(value, dict)


def _string(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise RestoreManifestError(f"manifest {field} must be a string")
    return value


def _string_row(
    value: JsonValue, fields: frozenset[str], section: str
) -> dict[str, str]:
    if not _record(value) or set(value) != set(fields):
        raise RestoreManifestError(f"manifest {section} row schema is invalid")
    return {field: _string(value[field], f"{section}.{field}") for field in fields}


def _string_rows(
    value: JsonValue, fields: frozenset[str], section: str
) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        raise RestoreManifestError(f"manifest {section} must be a list")
    return tuple(_string_row(row, fields, section) for row in value)


def _python_rows(value: JsonValue) -> tuple[dict[str, str | list[str]], ...]:
    if not isinstance(value, list):
        raise RestoreManifestError("manifest python must be a list")
    parsed: list[dict[str, str | list[str]]] = []
    for row in value:
        if not _record(row) or set(row) != {
            "scope",
            "executable",
            "version",
            "requirements",
        }:
            raise RestoreManifestError("manifest python row schema is invalid")
        requirements = row["requirements"]
        if not isinstance(requirements, list) or not all(
            isinstance(item, str) for item in requirements
        ):
            raise RestoreManifestError("manifest python requirements are invalid")
        parsed_requirements = [item for item in requirements if isinstance(item, str)]
        parsed.append(
            {
                "scope": _string(row["scope"], "python.scope"),
                "executable": _string(row["executable"], "python.executable"),
                "version": _string(row["version"], "python.version"),
                "requirements": parsed_requirements,
            }
        )
    return tuple(parsed)


def _parse_inventory(value: JsonValue) -> Inventory:
    fields = {"brewfile", "python", "npm", "pipx", "mas", "applications"}
    if not _record(value) or set(value) != fields:
        raise RestoreManifestError("manifest inventory schema is invalid")
    npm = value["npm"]
    if not _record(npm) or set(npm) != {"node_version", "prefix", "packages"}:
        raise RestoreManifestError("manifest npm schema is invalid")
    packages = _string_rows(
        npm["packages"], frozenset({"name", "version"}), "npm.packages"
    )
    return Inventory(
        _string(value["brewfile"], "inventory.brewfile"),
        _python_rows(value["python"]),
        {
            "node_version": _string(npm["node_version"], "npm.node_version"),
            "prefix": _string(npm["prefix"], "npm.prefix"),
            "packages": list(packages),
        },
        _string_rows(value["pipx"], frozenset({"name", "version"}), "pipx"),
        _string_rows(value["mas"], frozenset({"id", "name", "version"}), "mas"),
        _string_rows(
            value["applications"],
            frozenset({"name", "path", "bundle_id", "version"}),
            "applications",
        ),
    )


def _decode_json(
    raw: str, loader: Callable[[str], JsonValue] = json.loads
) -> JsonValue:
    return loader(raw)


def _safe_root(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RestoreManifestError(f"{label} root is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise RestoreManifestError(
            f"{label} root must be a safe regular directory: {path}"
        )


def _load_json(path: Path) -> JsonValue:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RestoreManifestError(f"manifest path is unsafe: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor) as stream:
            value = _decode_json(stream.read())
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreManifestError(f"manifest is unreadable or unsafe: {exc}") from exc
    return value


def _brewfile_artifact(
    root: Path,
    collectors: JsonValue,
    inventory: Inventory,
) -> Path | None:
    if not inventory.brewfile:
        return None
    if not isinstance(collectors, list):
        raise RestoreManifestError("manifest collectors schema is invalid")
    artifact = ""
    for collector in collectors:
        if _record(collector) and collector.get("id") == "brew":
            artifact = _string(collector.get("artifact"), "collectors.brew.artifact")
            break
    relative = PurePosixPath(artifact)
    if (
        not artifact
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RestoreManifestError("manifest Brewfile artifact path is unsafe")
    path = root.joinpath(*relative.parts)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RestoreManifestError("manifest Brewfile artifact is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RestoreManifestError("manifest Brewfile artifact is unsafe")
    return path


def load_inventory(path: Path, bot_id: str, manifest_id: str) -> LoadedInventory:
    _safe_root(path, "manifest")
    value = _load_json(path / "manifest.json")
    required = {
        "schema_version",
        "manifest_id",
        "bot",
        "captured_at",
        "host",
        "collectors",
        "inventory",
    }
    if not _record(value) or set(value) != required or value["schema_version"] != 1:
        raise RestoreManifestError("manifest top-level schema is invalid")
    bot = value["bot"]
    if (
        not _record(bot)
        or bot.get("id") != bot_id
        or value["manifest_id"] != manifest_id
    ):
        raise RestoreManifestError("manifest identity does not match requested Bot")
    inventory = _parse_inventory(value["inventory"])
    return LoadedInventory(
        manifest_id,
        path,
        inventory,
        _brewfile_artifact(path, value["collectors"], inventory),
    )


def load_latest_inventory(root: Path, bot_id: str) -> LoadedInventory:
    _safe_root(root, "environment")
    try:
        current = read_current_manifest(root, bot_id)
    except CurrentManifestError as exc:
        raise RestoreManifestError(str(exc)) from exc
    if current is None:
        raise RestoreManifestError("current manifest is unavailable")
    return load_inventory(current.path, bot_id, current.manifest_id)
