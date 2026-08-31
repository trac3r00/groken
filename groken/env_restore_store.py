from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Final

from .env_persistence import PersistenceError, validate_component
from .env_restore_errors import JournalConflictError, JournalUnsafeError
from .env_restore_journal import (
    JournalEntry,
    JournalState,
    JsonValue,
    RestoreJournal,
)
from .env_restore_journal_codec import decode_journal, entry_payload
from .env_restore_lock import restore_lock

__all__: Final = (
    "JournalConflictError",
    "JournalEntry",
    "JournalState",
    "JournalStore",
    "JournalUnsafeError",
    "JsonValue",
    "RestoreJournal",
)
_MANIFEST_ID: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _safe_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise JournalUnsafeError(
            f"journal path must be a safe regular directory: {path}"
        )
    path.chmod(0o700)


class JournalStore:
    def __init__(self, root: Path, bot_id: str, manifest_id: str) -> None:
        try:
            validate_component(bot_id)
        except PersistenceError as exc:
            raise JournalUnsafeError(str(exc)) from exc
        if _MANIFEST_ID.fullmatch(manifest_id) is None:
            raise JournalUnsafeError("journal manifest id is invalid")
        self.root: Path = root
        self.bot_id: str = bot_id
        self.manifest_id: str = manifest_id
        self.path: Path = root / bot_id / "journal" / f"{manifest_id}.json"
        self.lock_path: Path = self.path.with_suffix(".lock")

    def _prepare(self) -> None:
        _safe_directory(self.root)
        _safe_directory(self.root / self.bot_id)
        _safe_directory(self.path.parent)

    def load(self) -> RestoreJournal | None:
        self._prepare()
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            raise JournalUnsafeError(f"journal must be a regular file: {self.path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise JournalUnsafeError(f"journal file must use mode 0600: {self.path}")
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor) as stream:
                return decode_journal(stream.read(), self.bot_id, self.manifest_id)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JournalUnsafeError(f"journal is unreadable: {exc}") from exc

    def save(self, operations: tuple[JournalEntry, ...]) -> RestoreJournal:
        self._prepare()
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise JournalUnsafeError(f"journal must be a regular file: {self.path}")
        if len({row.key for row in operations}) != len(operations):
            raise JournalConflictError("journal contains duplicate operation keys")
        payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "bot_id": self.bot_id,
            "manifest_id": self.manifest_id,
            "operations": {row.key: entry_payload(row) for row in operations},
        }
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                encoded = (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
                _ = stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()
        return RestoreJournal(self.bot_id, self.manifest_id, operations)

    def ensure(self, requested: tuple[JournalEntry, ...]) -> RestoreJournal:
        current = self.load()
        if current is None:
            return self.save(requested)
        by_key = {row.key: row for row in current.operations}
        merged = list(current.operations)
        for row in requested:
            prior = by_key.get(row.key)
            if prior is not None and (prior.item != row.item or prior.argv != row.argv):
                raise JournalConflictError(
                    f"operation key {row.key!r} names a different operation"
                )
            if prior is None:
                merged.append(row)
        return self.save(tuple(merged))

    def lock(self) -> AbstractContextManager[None]:
        self._prepare()
        return restore_lock(self.lock_path)
