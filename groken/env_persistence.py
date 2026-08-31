from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeGuard

_SAFE_COMPONENT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


@dataclass(frozen=True, slots=True)
class TreeFile:
    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ManifestTree:
    manifest_id: str
    files: tuple[TreeFile, ...]


@dataclass(frozen=True, slots=True)
class MirrorTarget:
    local_root: Path
    bot_id: str


class PersistenceError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail


@dataclass(frozen=True, slots=True)
class CurrentManifestError(PersistenceError):
    detail: str

    def __post_init__(self) -> None:
        Exception.__init__(self, self.detail)


@dataclass(frozen=True, slots=True)
class CurrentManifest:
    manifest_id: str
    captured_at: datetime
    path: Path


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    files: dict[str, bytes]
    directories: frozenset[str]


def canonical_json_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def content_id(payload: dict[str, object], artifacts: tuple[TreeFile, ...]) -> str:
    """Hash canonical JSON without manifest_id, then sorted artifact path and bytes."""
    digest = hashlib.sha256(canonical_json_bytes(payload))
    for artifact in sorted(artifacts, key=lambda item: item.path):
        path = artifact.path.encode()
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(artifact.content).to_bytes(8, "big"))
        digest.update(artifact.content)
    return f"sha256:{digest.hexdigest()}"


def validate_component(bot_id: str) -> None:
    if _SAFE_COMPONENT.fullmatch(bot_id) is None:
        raise PersistenceError(f"unsafe bot id: {bot_id!r}")


def is_record(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _load_json(raw: str, loader: Callable[[str], object] = json.loads) -> object:
    return loader(raw)


def _regular_bytes(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise CurrentManifestError(f"path is not a regular file: {path}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise CurrentManifestError(f"unsafe manifest path: {path}") from exc


def read_current_manifest(root: Path, bot_id: str) -> CurrentManifest | None:
    """Parse current.json without escaping the root or following symlinks."""
    try:
        validate_component(bot_id)
    except PersistenceError as exc:
        raise CurrentManifestError(str(exc)) from exc
    bot_root = root / bot_id
    try:
        bot_metadata = bot_root.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(bot_metadata.st_mode):
        raise CurrentManifestError(f"Bot root is not a regular directory: {bot_root}")
    current_bytes = _regular_bytes(bot_root / "current.json")
    if current_bytes is None:
        return None
    try:
        current_value = _load_json(current_bytes.decode())
        if not is_record(current_value) or set(current_value) != {"manifest_id"}:
            raise CurrentManifestError("current record is corrupt")
        manifest_id = current_value["manifest_id"]
        if (
            not isinstance(manifest_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_id) is None
        ):
            raise CurrentManifestError("current manifest id is invalid")
        path = bot_root / manifest_id
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise CurrentManifestError(
                f"manifest path is not a regular directory: {path}"
            )
        try:
            manifest_bytes = _snapshot(path).files["manifest.json"]
        except (KeyError, PersistenceError) as exc:
            raise CurrentManifestError("manifest file is missing or unsafe") from exc
        value = _load_json(manifest_bytes.decode())
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CurrentManifestError(f"manifest record is unreadable: {exc}") from exc
    if not is_record(value):
        raise CurrentManifestError("manifest is not an object")
    bot = value.get("bot")
    captured = value.get("captured_at")
    if (
        value.get("schema_version") != 1
        or value.get("manifest_id") != manifest_id
        or not is_record(bot)
        or bot.get("id") != bot_id
        or not isinstance(captured, str)
    ):
        raise CurrentManifestError("manifest identity does not match current Bot")
    try:
        captured_at = datetime.fromisoformat(captured)
    except ValueError as exc:
        raise CurrentManifestError("manifest captured_at is invalid") from exc
    if captured_at.tzinfo is None:
        raise CurrentManifestError("manifest captured_at has no timezone")
    return CurrentManifest(manifest_id, captured_at.astimezone(UTC), path)


def _safe_directory(path: Path) -> None:
    if path.is_symlink():
        raise PersistenceError(f"refusing symlink path: {path}")
    if path.exists() and not path.is_dir():
        raise PersistenceError(f"path is not a directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        _ = stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _snapshot(root: Path) -> TreeSnapshot:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    relative = str(Path(entry.path).relative_to(root))
                    if entry.is_symlink():
                        raise PersistenceError(
                            "content id collision with existing manifest"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        directories.add(relative)
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        descriptor = os.open(entry.path, flags)
                        with os.fdopen(descriptor, "rb") as stream:
                            files[relative] = stream.read()
                    else:
                        raise PersistenceError(
                            "content id collision with existing manifest"
                        )
    except OSError as exc:
        raise PersistenceError("content id collision with existing manifest") from exc
    return TreeSnapshot(files, frozenset(directories))


def _matches_existing(target: Path, tree: ManifestTree) -> bool:
    snapshot = _snapshot(target)
    expected_files = {item.path: item.content for item in tree.files}
    expected_directories = {
        str(parent)
        for item in tree.files
        for parent in Path(item.path).parents
        if str(parent) != "."
    }
    return snapshot.files == expected_files and snapshot.directories == frozenset(
        expected_directories
    )


def mirror_tree(mirror: MirrorTarget, tree: ManifestTree) -> Path:
    validate_component(mirror.bot_id)
    _safe_directory(mirror.local_root)
    bot_root = mirror.local_root / mirror.bot_id
    _safe_directory(bot_root)
    temporary = Path(tempfile.mkdtemp(prefix=".capture-", dir=bot_root))
    temporary.chmod(0o700)
    target = bot_root / tree.manifest_id
    try:
        for item in tree.files:
            destination = temporary / item.path
            _safe_directory(destination.parent)
            _write_private(destination, item.content)
        try:
            _ = temporary.rename(target)
        except OSError:
            if (
                target.is_symlink()
                or not target.is_dir()
                or not _matches_existing(target, tree)
            ):
                raise PersistenceError(
                    "content id collision with existing manifest"
                ) from None
        current = bot_root / "current.json"
        if current.is_symlink():
            raise PersistenceError("refusing symlink current record")
        current_temp = bot_root / f".current-{uuid.uuid4().hex}.tmp"
        try:
            _write_private(
                current_temp, canonical_json_bytes({"manifest_id": tree.manifest_id})
            )
            os.replace(current_temp, current)
        finally:
            if current_temp.exists():
                current_temp.unlink()
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
