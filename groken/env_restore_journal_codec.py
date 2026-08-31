from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Final, TypeGuard
from urllib.parse import unquote

from .env_restore_errors import JournalUnsafeError
from .env_restore_journal import (
    JournalEntry,
    JournalState,
    JsonValue,
    RestoreJournal,
)
from .env_restore_validation import (
    RestoreInputError,
    item_key,
    parse_provider,
    scope_key,
    validate_identity,
)

_ENTRY_FIELDS: Final = {
    "item",
    "argv",
    "state",
    "attempts",
    "idempotency_key",
    "started_at",
    "ended_at",
    "exit_code",
    "signal",
    "truncated",
    "error",
}


def _record(value: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    return isinstance(value, dict)


def _decode(raw: str, loader: Callable[[str], JsonValue] = json.loads) -> JsonValue:
    return loader(raw)


def _optional_string(value: JsonValue) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise JournalUnsafeError("journal optional string field has invalid type")


def _optional_timestamp(value: JsonValue) -> str | None:
    parsed = _optional_string(value)
    if parsed is None:
        return None
    try:
        timestamp = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise JournalUnsafeError("journal timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise JournalUnsafeError("journal timestamp has no timezone")
    return parsed


def _parse_entry(key: str, value: JsonValue) -> JournalEntry:
    if not _record(value) or set(value) != _ENTRY_FIELDS:
        raise JournalUnsafeError("journal operation schema is invalid")
    item, argv = value["item"], value["argv"]
    attempts, exit_code = value["attempts"], value["exit_code"]
    signal, truncated = value["signal"], value["truncated"]
    if key.count("/") != 3 or not isinstance(item, str) or not isinstance(argv, list):
        raise JournalUnsafeError("journal operation identity is invalid")
    phase, raw_provider, raw_scope, raw_item = key.split("/")
    scope = unquote(raw_scope)
    try:
        provider = parse_provider(raw_provider)
        validate_identity(provider, scope, item)
    except RestoreInputError as exc:
        raise JournalUnsafeError(str(exc)) from exc
    if (
        phase != "restore"
        or unquote(raw_item) != item.lower()
        or raw_scope != scope_key(scope)
        or raw_item != item_key(item)
    ):
        raise JournalUnsafeError("journal operation key is invalid")
    if not all(isinstance(argument, str) for argument in argv):
        raise JournalUnsafeError("journal argv is invalid")
    parsed_argv = tuple(argument for argument in argv if isinstance(argument, str))
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise JournalUnsafeError("journal attempts is invalid")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        raise JournalUnsafeError("journal exit code is invalid")
    if signal is not None and (
        not isinstance(signal, int) or isinstance(signal, bool) or signal <= 0
    ):
        raise JournalUnsafeError("journal signal is invalid")
    if not isinstance(truncated, bool):
        raise JournalUnsafeError("journal truncated flag is invalid")
    try:
        state = JournalState(value["state"])
    except (TypeError, ValueError) as exc:
        raise JournalUnsafeError("journal state is invalid") from exc
    return JournalEntry(
        key,
        item,
        parsed_argv,
        state,
        attempts,
        _optional_string(value["idempotency_key"]),
        _optional_timestamp(value["started_at"]),
        _optional_timestamp(value["ended_at"]),
        exit_code,
        signal,
        truncated,
        _optional_string(value["error"]),
    )


def entry_payload(entry: JournalEntry) -> dict[str, JsonValue]:
    return {
        "item": entry.item,
        "argv": list(entry.argv),
        "state": entry.state.value,
        "attempts": entry.attempts,
        "idempotency_key": entry.idempotency_key,
        "started_at": entry.started_at,
        "ended_at": entry.ended_at,
        "exit_code": entry.exit_code,
        "signal": entry.signal,
        "truncated": entry.truncated,
        "error": entry.error,
    }


def decode_journal(raw: str, bot_id: str, manifest_id: str) -> RestoreJournal:
    value = _decode(raw)
    fields = {"schema_version", "bot_id", "manifest_id", "operations"}
    if not _record(value) or set(value) != fields:
        raise JournalUnsafeError("journal schema is invalid")
    if (
        value["schema_version"] != 1
        or value["bot_id"] != bot_id
        or value["manifest_id"] != manifest_id
    ):
        raise JournalUnsafeError("journal identity does not match its path")
    operations = value["operations"]
    if not _record(operations):
        raise JournalUnsafeError("journal operations schema is invalid")
    return RestoreJournal(
        bot_id,
        manifest_id,
        tuple(_parse_entry(key, row) for key, row in sorted(operations.items())),
    )
