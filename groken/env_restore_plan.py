from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from .env_collectors import Inventory
from .env_restore_inventory import InventoryIndex, InventoryItem, inventory_index
from .env_restore_validation import (
    Provider,
    RestoreInputError,
    item_key,
    python_argv,
    scope_key,
    validate_identity,
)


@dataclass(frozen=True, slots=True)
class RoutineRestore:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreOperation:
    phase: str
    provider: Provider
    scope: str
    item: str
    expected_version: str
    argv: tuple[str, ...]
    verifies: tuple[InventoryItem, ...]
    manual_reason: str | None = None

    @property
    def key(self) -> str:
        return f"{self.phase}/{self.provider.value}/{scope_key(self.scope)}/{item_key(self.item)}"


@dataclass(frozen=True, slots=True)
class RestoreRequest:
    expected: Inventory
    current: Inventory
    brewfile_path: Path | None
    routines: tuple[RoutineRestore, ...]
    brewfile_argv_path: Path | None = None


@dataclass(frozen=True, slots=True)
class RestorePlan:
    expected: InventoryIndex
    operations: tuple[RestoreOperation, ...]
    summary: str


def _python_operation(item: InventoryItem, expected: Inventory) -> RestoreOperation:
    candidate = next(
        (
            row.get("executable", "")
            for row in expected.python
            if row.get("scope") == item.scope
        ),
        "",
    )
    executable = candidate if isinstance(candidate, str) else ""
    requirement = f"{item.item}=={item.version}"
    return RestoreOperation(
        "restore",
        Provider.PYTHON,
        item.scope,
        item.item,
        item.version,
        python_argv(item.scope, executable, requirement),
        (item,),
    )


def _operation(
    item: InventoryItem, expected: Inventory, current: InventoryIndex
) -> RestoreOperation | None:
    if current.find(item.provider, item.scope, item.item) is not None:
        return None
    specs: dict[Provider, tuple[tuple[str, ...], str | None] | None] = {
        Provider.BREW: None,
        Provider.MAS: (("/usr/bin/env", "mas", "install", item.item), None),
        Provider.PYTHON: None,
        Provider.NPM: (
            (
                "/usr/bin/env",
                "npm",
                "install",
                "--global",
                f"{item.item}@{item.version}",
            ),
            None,
        ),
        Provider.PIPX: (
            (
                "/usr/bin/env",
                "pipx",
                "install",
                f"{item.item}=={item.version}",
            ),
            None,
        ),
        Provider.APPLICATION: (
            (),
            "application install, first launch, Gatekeeper, and login require manual action",
        ),
        Provider.ROUTINE: None,
    }
    if item.provider is Provider.PYTHON:
        return _python_operation(item, expected)
    spec = specs[item.provider]
    if spec is None:
        return None
    argv, manual = spec
    return RestoreOperation(
        "restore",
        item.provider,
        item.scope,
        item.item,
        item.version,
        argv,
        (item,),
        manual,
    )


def _brew_operation(
    expected: InventoryIndex,
    current: InventoryIndex,
    brewfile_path: Path | None,
    brewfile_argv_path: Path | None,
) -> RestoreOperation | None:
    items = tuple(row for row in expected.items if row.provider is Provider.BREW)
    missing = tuple(
        row for row in items if current.find(row.provider, row.scope, row.item) is None
    )
    if not missing:
        return None
    if brewfile_path is None:
        raise RestoreInputError("trusted Brewfile artifact is unavailable")
    try:
        metadata = brewfile_path.lstat()
    except FileNotFoundError as exc:
        raise RestoreInputError("trusted Brewfile artifact is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RestoreInputError("trusted Brewfile artifact is unsafe")
    argv_path = brewfile_argv_path or brewfile_path
    return RestoreOperation(
        "restore",
        Provider.BREW,
        "bundle",
        "Brewfile",
        "",
        ("/usr/bin/env", "brew", "bundle", "--file", str(argv_path)),
        items,
    )


def plan_restore(request: RestoreRequest) -> RestorePlan:
    expected = inventory_index(request.expected)
    current = inventory_index(request.current)
    operations = [
        row
        for item in expected.items
        if item.provider is not Provider.BREW
        and (row := _operation(item, request.expected, current)) is not None
    ]
    brew = _brew_operation(
        expected,
        current,
        request.brewfile_path,
        request.brewfile_argv_path,
    )
    if brew is not None:
        operations.insert(0, brew)
    for routine in sorted(request.routines, key=lambda row: row.name):
        validate_identity(Provider.ROUTINE, "env-restore", routine.name)
        operations.append(
            RestoreOperation(
                "restore",
                Provider.ROUTINE,
                "env-restore",
                routine.name,
                "",
                routine.argv,
                (),
            )
        )
    groups = (
        ("brew", Provider.BREW),
        ("mas", Provider.MAS),
        ("python", Provider.PYTHON),
        ("npm", Provider.NPM),
        ("pipx", Provider.PIPX),
        ("applications", Provider.APPLICATION),
    )
    lines: list[str] = []
    for group, provider in groups:
        lines.append(f"[{group}]")
        selected = [
            row
            for row in operations
            if row.provider is provider
            or (provider is Provider.APPLICATION and row.provider is Provider.ROUTINE)
        ]
        lines.extend(f"- {row.key} argv={json.dumps(row.argv)}" for row in selected)
        if not selected:
            lines.append("- none")
    return RestorePlan(expected, tuple(operations), "\n".join(lines))
