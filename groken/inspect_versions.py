"""Version selection and critical-contract comparison for app inspection."""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import (
    CURRENT_027_COMMAND_NAMES,
    CURRENT_030_COMMAND_NAMES,
    LEGACY_GATEWAY_COMMANDS,
)
from .gateway_versions import (
    CURRENT_027_CRITICAL_FINGERPRINTS,
    CURRENT_027_NO_ARGS,
    CURRENT_030_CRITICAL_FINGERPRINTS,
    CURRENT_030_NO_ARGS,
)
from .inspect_contracts import CommandContract


@dataclass(frozen=True, slots=True)
class ContractObservation:
    profile: str
    contracts: tuple[CommandContract, ...]
    expected_names: tuple[str, ...]
    expected_args: dict[str, str]
    reference_hash_match: bool


@dataclass(frozen=True, slots=True)
class ContractEvaluation:
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    unknown: tuple[str, ...]


def expected_contracts(
    bundle_version: str | None, embedded_version: str | None
) -> tuple[str, list[str], dict[str, str]]:
    """Select only a recognized packaging profile; unknown never means legacy."""
    if bundle_version is None or embedded_version is None:
        return "unknown", [], {}
    if bundle_version.startswith("0.30.") and embedded_version.startswith("0.30."):
        names = list(CURRENT_030_COMMAND_NAMES)
        args = {
            name: "none" if name in CURRENT_030_NO_ARGS else "object" for name in names
        }
        return "grok-bot-0.30", names, args
    if bundle_version.startswith("0.27.") and embedded_version.startswith("0.27."):
        names = list(CURRENT_027_COMMAND_NAMES)
        args = {
            name: "none" if name in CURRENT_027_NO_ARGS else "object" for name in names
        }
        return "grok-bot-0.27", names, args
    if embedded_version.startswith("0.24.") and bundle_version.startswith(
        ("0.24.", "0.27.", "0.30.")
    ):
        return (
            "legacy-embedded-0.24",
            [spec.name for spec in LEGACY_GATEWAY_COMMANDS],
            {spec.name: spec.args for spec in LEGACY_GATEWAY_COMMANDS},
        )
    return "unknown", [], {}


def evaluate_contracts(observation: ContractObservation) -> ContractEvaluation:
    """Compare profile-specific critical fingerprints when bundle hashes differ."""
    critical_fingerprints = {
        "grok-bot-0.27": CURRENT_027_CRITICAL_FINGERPRINTS,
        "grok-bot-0.30": CURRENT_030_CRITICAL_FINGERPRINTS,
    }.get(observation.profile, {})
    by_name = {contract.name: contract for contract in observation.contracts}
    changed: list[str] = []
    unchanged: list[str] = []
    for name in sorted(set(by_name) & set(observation.expected_names)):
        contract = by_name[name]
        handler_matches = contract.handler_target in {None, name}
        args_match = contract.args == observation.expected_args[name]
        if not handler_matches or not args_match:
            changed.append(name)
            continue
        baseline = critical_fingerprints.get(name)
        if (
            observation.reference_hash_match
            or baseline is None
            or contract.fingerprint == baseline
        ):
            unchanged.append(name)
        else:
            changed.append(name)
    unknown = sorted(
        set(critical_fingerprints) & set(observation.expected_names) - set(by_name)
    )
    return ContractEvaluation(tuple(changed), tuple(unchanged), tuple(unknown))
