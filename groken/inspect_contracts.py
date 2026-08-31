"""Deterministic fingerprints for known minified gateway table shapes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

ContractArgs = Literal["none", "object", "unknown"]
ContractKind = Literal["handler", "validator"]

_NAME = r"[A-Za-z_$][A-Za-z0-9_$]*"
_HANDLER_ENTRY = re.compile(
    rf"(?P<name>{_NAME}):(?P<async>async\s*)?(?P<params>\([^)]*\)|{_NAME})=>{_NAME}\.(?P<target>{_NAME})\("
)
_VALIDATOR_ENTRY = re.compile(
    rf"(?P<name>{_NAME}):{_NAME}\(\)\.(?P<mode>noArgs|args\()"
)
_HANDLER_GAP = 10
_VALIDATOR_GAP = 512


@dataclass(frozen=True, slots=True)
class CommandContract:
    name: str
    args: ContractArgs
    fingerprint: str
    kind: ContractKind
    handler_target: str | None


@dataclass(frozen=True, slots=True)
class ContractSignatureDiff:
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    unknown: tuple[str, ...]


def _balanced_end(source: str, opening: int) -> int | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for index in range(opening, len(source)):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            _ = stack.pop()
            if not stack:
                return index + 1
    return None


def _largest_group(matches: list[re.Match[str]], gap: int) -> list[re.Match[str]]:
    groups: list[list[re.Match[str]]] = []
    current: list[re.Match[str]] = []
    previous_end: int | None = None
    for match in matches:
        if previous_end is None or match.start() - previous_end <= gap:
            current.append(match)
        else:
            groups.append(current)
            current = [match]
        previous_end = match.end()
    if current:
        groups.append(current)
    if not groups:
        return []
    return max(groups, key=len)


def _fingerprint(fragment: str) -> str:
    normalized = re.sub(r"\s+", "", fragment)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _handler_contracts(source: str) -> tuple[CommandContract, ...]:
    matches = _largest_group(list(_HANDLER_ENTRY.finditer(source)), _HANDLER_GAP)
    contracts: list[CommandContract] = []
    for match in matches:
        opening = match.end() - 1
        end = _balanced_end(source, opening)
        if end is None:
            continue
        params = match.group("params")
        args: ContractArgs = "object" if "," in params else "none"
        contracts.append(
            CommandContract(
                name=match.group("name"),
                args=args,
                fingerprint=_fingerprint(source[match.start() : end]),
                kind="handler",
                handler_target=match.group("target"),
            )
        )
    return tuple(contracts)


def _validator_contracts(source: str) -> tuple[CommandContract, ...]:
    matches = _largest_group(list(_VALIDATOR_ENTRY.finditer(source)), _VALIDATOR_GAP)
    contracts: list[CommandContract] = []
    for match in matches:
        mode = match.group("mode")
        end = match.end()
        if mode != "noArgs":
            balanced_end = _balanced_end(source, end - 1)
            if balanced_end is None:
                continue
            end = balanced_end
        contracts.append(
            CommandContract(
                name=match.group("name"),
                args="none" if mode == "noArgs" else "object",
                fingerprint=_fingerprint(source[match.start() : end]),
                kind="validator",
                handler_target=None,
            )
        )
    return tuple(contracts)


def extract_command_contracts(source: str) -> tuple[CommandContract, ...]:
    """Extract the largest known dispatch or validator table without parsing JS."""
    handlers = _handler_contracts(source)
    validators = _validator_contracts(source)
    return validators if len(validators) >= len(handlers) else handlers


def diff_contract_signatures(
    *,
    found: tuple[CommandContract, ...],
    expected: tuple[CommandContract, ...],
    found_names: tuple[str, ...] | None = None,
) -> ContractSignatureDiff:
    """Compare exact extracted fingerprints; missing bodies remain unknown."""
    found_by_name = {contract.name: contract for contract in found}
    expected_by_name = {contract.name: contract for contract in expected}
    observed_names = set(found_names) if found_names is not None else set(found_by_name)
    changed: list[str] = []
    unchanged: list[str] = []
    unknown: list[str] = []
    for name in sorted(observed_names & set(expected_by_name)):
        actual = found_by_name.get(name)
        baseline = expected_by_name[name]
        if actual is None:
            unknown.append(name)
        elif actual.fingerprint == baseline.fingerprint:
            unchanged.append(name)
        else:
            changed.append(name)
    return ContractSignatureDiff(tuple(changed), tuple(unchanged), tuple(unknown))
