from __future__ import annotations

import shlex
from dataclasses import dataclass

from .env_collectors import Inventory
from .env_restore_validation import (
    Provider,
    RestoreInputError,
    parse_python_requirement,
    validate_identity,
)


@dataclass(frozen=True, slots=True)
class InventoryItem:
    provider: Provider
    scope: str
    item: str
    version: str


@dataclass(frozen=True, slots=True)
class InventoryIndex:
    items: tuple[InventoryItem, ...]

    def find(self, provider: Provider, scope: str, item: str) -> InventoryItem | None:
        normalized = item.casefold()
        return next(
            (
                row
                for row in self.items
                if row.provider is provider
                and row.scope == scope
                and row.item.casefold() == normalized
            ),
            None,
        )


def _rows(value: str | list[dict[str, str]]) -> list[dict[str, str]]:
    return value if isinstance(value, list) else []


def _add(
    items: list[InventoryItem], provider: Provider, scope: str, item: str, version: str
) -> None:
    validate_identity(provider, scope, item)
    items.append(InventoryItem(provider, scope, item, version))


def inventory_index(inventory: Inventory) -> InventoryIndex:
    items: list[InventoryItem] = []
    for line in inventory.brewfile.splitlines():
        try:
            fields = shlex.split(line, comments=True)
        except ValueError as exc:
            raise RestoreInputError("invalid Brewfile syntax") from exc
        if len(fields) >= 2 and fields[0] in {"brew", "cask"}:
            _add(items, Provider.BREW, fields[0], fields[1].rstrip(","), "")
    for row in inventory.mas:
        _add(
            items, Provider.MAS, "app-store", row.get("id", ""), row.get("version", "")
        )
    for row in inventory.python:
        raw_scope = row.get("scope", "")
        scope = raw_scope if isinstance(raw_scope, str) else ""
        requirements = row.get("requirements", [])
        if not isinstance(requirements, list):
            raise RestoreInputError("invalid Python requirements list")
        for requirement in requirements:
            name, version = parse_python_requirement(requirement)
            _add(items, Provider.PYTHON, scope, name, version)
    for row in _rows(inventory.npm.get("packages", [])):
        _add(items, Provider.NPM, "global", row.get("name", ""), row.get("version", ""))
    for row in inventory.pipx:
        _add(
            items, Provider.PIPX, "global", row.get("name", ""), row.get("version", "")
        )
    for row in inventory.applications:
        identity = row.get("bundle_id", "") or row.get("name", "")
        _add(
            items,
            Provider.APPLICATION,
            "applications",
            identity,
            row.get("version", ""),
        )
    return InventoryIndex(tuple(items))
