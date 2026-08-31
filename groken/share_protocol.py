"""Typed errors and SSE framing for the share relay protocol."""

from __future__ import annotations

import json
from collections.abc import Iterator


class SharePermissionError(PermissionError):
    """Raised when a relay-local bot mutation is requested."""


class ShareProtocolError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ShareRemoteError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class EventFeed:
    def __init__(self, lines: Iterator[str]) -> None:
        self._lines = lines
        self._event = "message"

    def __iter__(self) -> Iterator[dict[str, object]]:
        while True:
            try:
                yield self.next_event(None)
            except StopIteration:
                return

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del timeout_s, hold
        for line in self._lines:
            if line.startswith("event:"):
                self._event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    value: object = json.loads(data)
                    event: dict[str, object] = {
                        "event": self._event,
                        "data": value,
                    }
                    self._event = "message"
                    return event
        raise StopIteration

    def resume(self) -> None:
        return None
