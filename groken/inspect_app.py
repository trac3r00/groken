"""Read-only inspection of versioned Grok Bot gateway contracts."""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict, cast

from .app_archive import (
    DEFAULT_APP_PATH,
    HOST_MAIN,
    AsarError,
    AsarHeader,
    read_asar_entry,
    read_asar_header,
    read_bundle_version,
    read_host_blob,
    read_package_metadata,
    resolve_asar,
    sha256_file,
)
from .capabilities import CURRENT_030_COMMAND_NAMES, LEGACY_GATEWAY_COMMANDS
from .gateway_versions import (
    CURRENT_027_ASAR_SHA256,
    CURRENT_027_HOST_SHA256,
    CURRENT_030_ASAR_SHA256,
    CURRENT_030_HOST_SHA256,
)
from .inspect_contracts import extract_command_contracts
from .inspect_versions import (
    ContractObservation,
    evaluate_contracts,
    expected_contracts,
)

_RENAME_THRESHOLD = 0.8
_SERVICE_HEAD = re.compile(r'typeName:"(agent\.v1\.[A-Za-z0-9_]+)",methods:\{')
_METHOD_NAME = re.compile(r'name:"([A-Za-z0-9_]+)"')
_MAX_SERVICE_BLOCK = 200_000

__all__ = (
    "HOST_MAIN",
    "AsarError",
    "AsarHeader",
    "diff_commands",
    "expected_contracts",
    "extract_command_names",
    "extract_service_methods",
    "inspect_app",
    "read_asar_entry",
    "read_asar_header",
)

JsonObject = dict[str, object]


class CommandDrift(TypedDict):
    added: list[str]
    removed: list[str]
    renamed: list[dict[str, str]]
    changed: list[str]
    unchanged: list[str]
    unknown: list[str]
    names_verified: bool
    schemas_verified: bool
    clean: bool


class InspectionReport(TypedDict):
    app_path: str
    asar: str
    bundle_version: str | None
    embedded_package_version: str | None
    app_version: str | None
    asar_sha256: str
    host_main: str
    host_main_bytes: int
    host_main_sha256: str
    expected_profile: str
    reference_hash_match: bool
    command_count: int
    expected_count: int
    commands: list[str]
    command_contracts: dict[str, JsonObject]
    services: dict[str, list[str]]
    service_drift: JsonObject
    legacy_delta: JsonObject
    drift: CommandDrift
    warnings: list[str]


def extract_command_names(source: str) -> list[str]:
    """Return names from the largest recognized gateway contract table."""
    return sorted({contract.name for contract in extract_command_contracts(source)})


def extract_service_methods(source: str) -> dict[str, list[str]]:
    """Map recognized ``agent.v1.*`` services to RPC method names."""
    services: dict[str, list[str]] = {}
    for head in _SERVICE_HEAD.finditer(source):
        start, depth, index = head.end(), 1, head.end()
        limit = min(len(source), start + _MAX_SERVICE_BLOCK)
        while index < limit and depth:
            depth += (source[index] == "{") - (source[index] == "}")
            index += 1
        if depth:
            continue
        methods = sorted(set(_METHOD_NAME.findall(source[start : index - 1])))
        if methods:
            services[head.group(1)] = methods
    return services


def diff_commands(*, found: list[str], expected: list[str]) -> JsonObject:
    """Diff command names and identify likely renames without claiming schemas."""
    found_set, expected_set = set(found), set(expected)
    added, removed = sorted(found_set - expected_set), sorted(expected_set - found_set)
    pairs = sorted(
        (
            (SequenceMatcher(None, gone, new).ratio(), gone, new)
            for gone in removed
            for new in added
            if SequenceMatcher(None, gone, new).ratio() >= _RENAME_THRESHOLD
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    renamed: list[dict[str, str]] = []
    used_old: set[str] = set()
    used_new: set[str] = set()
    for _, gone, new in pairs:
        if gone not in used_old and new not in used_new:
            used_old.add(gone)
            used_new.add(new)
            renamed.append({"from": gone, "to": new})
    remaining_added = [name for name in added if name not in used_new]
    remaining_removed = [name for name in removed if name not in used_old]
    return {
        "added": remaining_added,
        "removed": remaining_removed,
        "renamed": sorted(renamed, key=lambda item: item["from"]),
        "clean": not (remaining_added or remaining_removed or renamed),
    }


def inspect_app(app_path: Path | str | None = None) -> InspectionReport:
    """Inspect versioned names and fingerprints without asserting reply schemas."""
    root = Path(app_path) if app_path is not None else DEFAULT_APP_PATH
    archive = resolve_asar(root)
    header = read_asar_header(archive)
    embedded_version, package_main = read_package_metadata(archive, header)
    bundle_version = read_bundle_version(root)
    host_main, host_blob, contracts = read_host_blob(archive, header, package_main)
    host_hash = hashlib.sha256(host_blob).hexdigest()
    asar_hash = sha256_file(archive)
    found = sorted({contract.name for contract in contracts})
    expected_profile, expected_names, expected_args = expected_contracts(
        bundle_version, embedded_version
    )
    reference_hashes = {
        "grok-bot-0.27": (CURRENT_027_ASAR_SHA256, CURRENT_027_HOST_SHA256),
        "grok-bot-0.30": (CURRENT_030_ASAR_SHA256, CURRENT_030_HOST_SHA256),
    }.get(expected_profile)
    reference_hash_match = reference_hashes == (asar_hash, host_hash)
    names = diff_commands(found=found, expected=expected_names)
    by_name = {contract.name: contract for contract in contracts}
    evaluation = evaluate_contracts(
        ContractObservation(
            expected_profile,
            contracts,
            tuple(expected_names),
            expected_args,
            reference_hash_match,
        )
    )
    drift: CommandDrift = {
        "added": cast("list[str]", names["added"]),
        "removed": cast("list[str]", names["removed"]),
        "renamed": cast("list[dict[str, str]]", names["renamed"]),
        "changed": list(evaluation.changed),
        "unchanged": list(evaluation.unchanged),
        "unknown": list(evaluation.unknown),
        "names_verified": True,
        "schemas_verified": False,
        "clean": bool(
            names["clean"] and not evaluation.changed and not evaluation.unknown
        ),
    }
    services = extract_service_methods(host_blob.decode("utf-8", "replace"))
    warnings = [
        "Command names are verified; validator details and reply schemas are not fully verified.",
        "A clean name diff must not be interpreted as validator or reply compatibility.",
    ]
    if expected_profile == "unknown":
        warnings.append("No versioned expectation matches the bundle/package versions.")
    elif not reference_hash_match:
        warnings.append(
            f"Bundle hashes differ from the {expected_profile.removeprefix('grok-bot-')} "
            "reference; critical fingerprints were compared."
        )
    return {
        "app_path": str(root),
        "asar": str(archive),
        "bundle_version": bundle_version,
        "embedded_package_version": embedded_version,
        "app_version": embedded_version,
        "asar_sha256": asar_hash,
        "host_main": host_main,
        "host_main_bytes": len(host_blob),
        "host_main_sha256": host_hash,
        "expected_profile": expected_profile,
        "reference_hash_match": reference_hash_match,
        "command_count": len(found),
        "expected_count": len(expected_names),
        "commands": found,
        "command_contracts": {
            name: {
                "args": contract.args,
                "fingerprint": contract.fingerprint,
                "kind": contract.kind,
                "handler_target": contract.handler_target,
                "schema_confidence": (
                    "partial" if contract.kind == "validator" else "unknown"
                ),
            }
            for name, contract in sorted(by_name.items())
        },
        "services": services,
        "service_drift": {
            "status": "observed" if services else "unknown",
            "schemas_verified": False,
            "warning": "No versioned service-schema baseline is available.",
        },
        "legacy_delta": {
            "added": sorted(
                set(CURRENT_030_COMMAND_NAMES)
                - {spec.name for spec in LEGACY_GATEWAY_COMMANDS}
            ),
            "removed": sorted(
                {spec.name for spec in LEGACY_GATEWAY_COMMANDS}
                - set(CURRENT_030_COMMAND_NAMES)
            ),
            "unchanged_count": len(
                set(CURRENT_030_COMMAND_NAMES)
                & {spec.name for spec in LEGACY_GATEWAY_COMMANDS}
            ),
        },
        "drift": drift,
        "warnings": warnings,
    }
