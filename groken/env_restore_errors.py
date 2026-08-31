from __future__ import annotations


class JournalUnsafeError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail


class JournalConflictError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail
