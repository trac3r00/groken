from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeAlias, TypeGuard

from .env_restore_errors import JournalConflictError, JournalUnsafeError

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
_PROCESS_IDENTITY: Final = uuid.uuid4().hex
_PROCESS_STARTED_AT: Final = datetime.now(UTC).isoformat()
_LOCK_FIELDS: Final = {
    "schema_version",
    "pid",
    "hostname",
    "process_identity",
    "started_at",
}


@dataclass(frozen=True, slots=True)
class LockOwner:
    pid: int
    hostname: str
    process_identity: str
    started_at: str


def _record(value: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    return isinstance(value, dict)


def _decode(raw: str, loader: Callable[[str], JsonValue] = json.loads) -> JsonValue:
    return loader(raw)


def _owner(raw: str) -> LockOwner:
    try:
        value = _decode(raw)
    except json.JSONDecodeError as exc:
        raise JournalUnsafeError("restore lock metadata is malformed") from exc
    if not _record(value) or set(value) != _LOCK_FIELDS or value["schema_version"] != 1:
        raise JournalUnsafeError("restore lock metadata schema is invalid")
    pid = value["pid"]
    hostname = value["hostname"]
    identity = value["process_identity"]
    started_at = value["started_at"]
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(hostname, str)
        or not isinstance(identity, str)
        or not identity
        or not isinstance(started_at, str)
    ):
        raise JournalUnsafeError("restore lock owner metadata is invalid")
    try:
        parsed = datetime.fromisoformat(started_at)
    except ValueError as exc:
        raise JournalUnsafeError("restore lock start timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise JournalUnsafeError("restore lock start timestamp has no timezone")
    return LockOwner(pid, hostname, identity, started_at)


def _read(descriptor: int) -> str:
    _ = os.lseek(descriptor, 0, os.SEEK_SET)
    return os.read(descriptor, 16_384).decode()


def _write_owner(descriptor: int) -> None:
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_identity": _PROCESS_IDENTITY,
        "started_at": _PROCESS_STARTED_AT,
    }
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    _ = os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    _ = os.write(descriptor, encoded)
    os.fsync(descriptor)


def _open(path: Path) -> tuple[int, bool]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        existed = False
    else:
        existed = True
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise JournalUnsafeError(
                f"restore lock must be a 0600 regular file: {path}"
            )
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise JournalUnsafeError(f"restore lock path is unsafe: {path}") from exc
    os.fchmod(descriptor, 0o600)
    return descriptor, existed


@contextmanager
def restore_lock(path: Path) -> Generator[None, None, None]:
    descriptor, existed = _open(path)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            owner = _owner(_read(descriptor))
            raise JournalConflictError(
                f"restore is already in progress by pid {owner.pid} on {owner.hostname}"
            ) from exc
        if existed:
            owner = _owner(_read(descriptor))
            if owner.hostname != socket.gethostname():
                raise JournalConflictError(
                    f"restore lock belongs to foreign host {owner.hostname!r}"
                )
        _write_owner(descriptor)
        yield
    finally:
        if acquired:
            try:
                current = path.lstat()
            except FileNotFoundError:
                current = None
            if (
                current is not None
                and stat.S_ISREG(current.st_mode)
                and current.st_ino == os.fstat(descriptor).st_ino
            ):
                path.unlink()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
