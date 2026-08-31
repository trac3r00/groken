"""Durable, revocable Bearer tokens for the share relay."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_TOKEN_PREFIX: Final = "grk_share_"
_DEFAULT_PATH: Final = Path.home() / ".config" / "groken" / "shares.json"


@dataclass(frozen=True, slots=True)
class ShareRecord:
    name: str
    bot_id: str
    bot_name: str
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class ShareGrant:
    token: str
    record: ShareRecord


class DuplicateShareError(Exception):
    __slots__: tuple[str, ...] = ("name",)

    name: str

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"share name already exists: {self.name}"


@dataclass(frozen=True, slots=True)
class ShareNotFoundError(Exception):
    name: str

    def __str__(self) -> str:
        return f"share not found: {self.name}"


@dataclass(frozen=True, slots=True)
class BlankShareFieldError(Exception):
    field: str

    def __str__(self) -> str:
        return f"share {self.field} must not be blank"


class ShareStoreDataError(Exception):
    __slots__: tuple[str, ...] = ("path", "reason")

    path: Path
    reason: str

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(path, reason)
        self.path = path
        self.reason = reason

    def __str__(self) -> str:
        return f"share store at {self.path} is {self.reason}; repair or remove the file"


@dataclass(frozen=True, slots=True)
class _StoredGrant:
    record: ShareRecord
    token_hash: str


class ShareStore:
    """Store share grants as hashes while returning plaintext tokens once."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self._lock_path = Path(f"{self.path}.lock")

    @contextmanager
    def _transaction_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> list[_StoredGrant]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ShareStoreDataError(self.path, "malformed") from exc
        if not isinstance(payload, list):
            raise ShareStoreDataError(self.path, "not a list")
        grants: list[_StoredGrant] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            bot_id = item.get("bot_id")
            bot_name = item.get("bot_name")
            token_hash = item.get("token_hash")
            revoked = item.get("revoked", False)
            if (
                isinstance(name, str)
                and isinstance(bot_id, str)
                and isinstance(bot_name, str)
                and isinstance(token_hash, str)
                and len(token_hash) == 64
                and all(character in "0123456789abcdef" for character in token_hash)
                and isinstance(revoked, bool)
            ):
                grants.append(
                    _StoredGrant(
                        ShareRecord(name, bot_id, bot_name, revoked), token_hash
                    )
                )
        return grants

    def _write(self, grants: list[_StoredGrant]) -> None:
        payload = [
            {
                "name": grant.record.name,
                "bot_id": grant.record.bot_id,
                "bot_name": grant.record.bot_name,
                "revoked": grant.record.revoked,
                "token_hash": grant.token_hash,
            }
            for grant in grants
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _require_nonblank(field: str, value: str) -> None:
        if not value.strip():
            raise BlankShareFieldError(field)

    def create(self, name: str, bot_id: str, bot_name: str) -> ShareGrant:
        """Create a grant pinned to one immutable Bot identity."""
        for field, value in (
            ("name", name),
            ("bot_id", bot_id),
            ("bot_name", bot_name),
        ):
            self._require_nonblank(field, value)
        with self._transaction_lock():
            grants = self._read()
            if any(grant.record.name == name for grant in grants):
                raise DuplicateShareError(name)
            token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
            record = ShareRecord(name, bot_id, bot_name)
            grants.append(_StoredGrant(record, self._hash(token)))
            self._write(grants)
        return ShareGrant(token, record)

    def list(self) -> list[ShareRecord]:
        """Return grants without exposing token material."""
        return [grant.record for grant in self._read()]

    def authenticate(self, token: str) -> ShareRecord | None:
        """Resolve an active token using constant-time hash comparison."""
        candidate = self._hash(token)
        try:
            grants = self._read()
        except ShareStoreDataError:
            return None
        for grant in grants:
            if hmac.compare_digest(candidate, grant.token_hash):
                return None if grant.record.revoked else grant.record
        return None

    def revoke(self, name: str) -> ShareRecord:
        """Revoke a grant by name and return its resulting record."""
        with self._transaction_lock():
            grants = self._read()
            for index, grant in enumerate(grants):
                if grant.record.name == name:
                    record = ShareRecord(
                        grant.record.name,
                        grant.record.bot_id,
                        grant.record.bot_name,
                        True,
                    )
                    grants[index] = _StoredGrant(record, grant.token_hash)
                    self._write(grants)
                    return record
        raise ShareNotFoundError(name)
