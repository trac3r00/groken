"""Bounded JSON encoding for untrusted peer-result relay data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

MAX_PEER_PAYLOAD_BYTES: Final = 12_000
MAX_PEER_LABEL_BYTES: Final = 256
PEER_BLOCK_START: Final = "<<<BEGIN_PEER_OUTPUT_DATA>>>"
PEER_BLOCK_END: Final = "<<<END_PEER_OUTPUT_DATA>>>"
TRUNCATION_MARK: Final = "...[truncated]"


@dataclass(frozen=True, slots=True)
class RelayEntry:
    bot: str
    status: Literal["answer", "failure"]
    content: str


def _json_size(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


def _bounded_text(text: str, budget: int) -> str:
    sanitized = text.replace(PEER_BLOCK_START, "[BEGIN marker escaped]").replace(
        PEER_BLOCK_END, "[END marker escaped]"
    )
    if _json_size(sanitized) <= budget:
        return sanitized
    if _json_size(TRUNCATION_MARK) > budget:
        return ""
    low, high = 0, len(sanitized)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = sanitized[:middle] + TRUNCATION_MARK
        if _json_size(candidate) <= budget:
            low = middle
        else:
            high = middle - 1
    return sanitized[:low] + TRUNCATION_MARK


def encode_peer_data(entries: tuple[RelayEntry, ...]) -> str:
    """Encode every peer field deterministically within one aggregate byte budget."""
    empty = [{"bot": "", "status": entry.status, "content": ""} for entry in entries]
    base_size = len(
        json.dumps(empty, ensure_ascii=False, separators=(",", ":")).encode()
    )
    available = max(0, MAX_PEER_PAYLOAD_BYTES - base_size)
    count = len(entries)
    share, remainder = divmod(available, count) if count else (0, 0)
    payload: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        entry_budget = share + (1 if index < remainder else 0)
        bot = _bounded_text(
            entry.bot,
            min(MAX_PEER_LABEL_BYTES, entry_budget // 3),
        )
        content = _bounded_text(
            entry.content,
            max(0, entry_budget - _json_size(bot)),
        )
        payload.append({"bot": bot, "status": entry.status, "content": content})
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
