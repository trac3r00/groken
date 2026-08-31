from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote


class Provider(StrEnum):
    BREW = "brew"
    MAS = "mas"
    PYTHON = "python"
    NPM = "npm"
    PIPX = "pipx"
    APPLICATION = "application"
    ROUTINE = "routine"


class RestoreInputError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail


_BREW_ITEM: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.@/-]*\Z")
_MAS_ITEM: Final = re.compile(r"[0-9]+\Z")
_PYTHON_ITEM: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_NPM_ITEM: Final = re.compile(
    r"(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)
_ROUTINE_ITEM: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_VERSION: Final = re.compile(r"[^\s\x00-\x1f\x7f]+\Z")
_EXECUTABLE_PREFIXES: Final = ("/usr/bin/", "/opt/homebrew/bin/", "/usr/local/bin/")
_FIXED_SCOPES: Final = {
    Provider.BREW: frozenset({"brew", "cask", "bundle"}),
    Provider.MAS: frozenset({"app-store"}),
    Provider.NPM: frozenset({"global"}),
    Provider.PIPX: frozenset({"global"}),
    Provider.APPLICATION: frozenset({"applications"}),
    Provider.ROUTINE: frozenset({"env-restore"}),
}
_PATTERNS: Final = {
    Provider.BREW: _BREW_ITEM,
    Provider.MAS: _MAS_ITEM,
    Provider.PYTHON: _PYTHON_ITEM,
    Provider.NPM: _NPM_ITEM,
    Provider.PIPX: _PYTHON_ITEM,
    Provider.ROUTINE: _ROUTINE_ITEM,
}


def _plain_item(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and not value.startswith("-")
        and all(character.isprintable() for character in value)
    )


def _safe_venv(scope: str) -> str | None:
    prefix = "venv:workspace/"
    if not scope.startswith(prefix) or "\\" in scope:
        return None
    relative = scope.removeprefix("venv:")
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None
    return relative


def validate_scope(provider: Provider, scope: str) -> None:
    if provider is Provider.PYTHON:
        if scope in {"system", "user"} or _safe_venv(scope) is not None:
            return
        raise RestoreInputError(f"unsafe Python scope: {scope!r}")
    allowed = _FIXED_SCOPES.get(provider)
    if allowed is None or scope not in allowed:
        raise RestoreInputError(f"invalid {provider.value} scope: {scope!r}")


def validate_item(provider: Provider, item: str) -> None:
    if not _plain_item(item):
        raise RestoreInputError(f"unsafe {provider.value} item: {item!r}")
    pattern = _PATTERNS.get(provider)
    if pattern is not None and pattern.fullmatch(item) is None:
        raise RestoreInputError(f"invalid {provider.value} item: {item!r}")
    if provider is Provider.BREW and any(
        part in {".", ".."} for part in item.split("/")
    ):
        raise RestoreInputError(f"unsafe brew item: {item!r}")


def validate_identity(provider: Provider, scope: str, item: str) -> None:
    validate_scope(provider, scope)
    validate_item(provider, item)


def parse_provider(value: str) -> Provider:
    try:
        return Provider(value)
    except ValueError as exc:
        raise RestoreInputError(f"invalid restore provider: {value!r}") from exc


def parse_python_requirement(value: str) -> tuple[str, str]:
    name, marker, version = value.partition("==")
    validate_item(Provider.PYTHON, name)
    if marker != "==" or _VERSION.fullmatch(version) is None:
        raise RestoreInputError(f"invalid pinned Python requirement: {value!r}")
    return name, version


def python_argv(
    scope: str, captured_executable: str, requirement: str
) -> tuple[str, ...]:
    validate_scope(Provider.PYTHON, scope)
    if scope == "system" or scope == "user":
        if not captured_executable.startswith(_EXECUTABLE_PREFIXES):
            raise RestoreInputError(
                f"unsafe Python executable: {captured_executable!r}"
            )
        user_flag = ("--user",) if scope == "user" else ()
        return (
            "/usr/bin/env",
            captured_executable,
            "-m",
            "pip",
            "install",
            *user_flag,
            requirement,
        )
    relative = _safe_venv(scope)
    if relative is None:
        raise RestoreInputError(f"unsafe Python scope: {scope!r}")
    return (
        "/usr/bin/env",
        f"/{relative}/bin/python",
        "-m",
        "pip",
        "install",
        requirement,
    )


def scope_key(scope: str) -> str:
    return quote(scope, safe="._-@")


def item_key(item: str) -> str:
    return quote(item.lower(), safe="._-@")
