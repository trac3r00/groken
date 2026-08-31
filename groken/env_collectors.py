from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, TypeVar

from .env_persistence import ManifestTree

T = TypeVar("T")
_TRUNCATED_REASON: Final = (
    "native command output truncated; reduce collector output and recapture"
)


class NativePlaneUnavailable(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NativeAdapterError(Exception):
    """Native controller data or adapter behavior violated the capture contract."""


@dataclass(frozen=True, slots=True)
class CommandRequest:
    argv: tuple[str, ...]
    stdin: bytes = b""
    timeout_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class _TruncatedCommandError(Exception):
    result: CommandResult


class CommandRunner(Protocol):
    def run(self, request: CommandRequest) -> CommandResult: ...


class NativeRunner(CommandRunner, Protocol):
    def publish(self, tree: ManifestTree) -> None: ...


class CollectorStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CollectorOutput:
    id: str
    status: CollectorStatus
    artifact: bytes
    command: tuple[str, ...]
    exit_code: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class Inventory:
    brewfile: str
    python: tuple[dict[str, str | list[str]], ...]
    npm: dict[str, str | list[dict[str, str]]]
    pipx: tuple[dict[str, str], ...]
    mas: tuple[dict[str, str], ...]
    applications: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class CollectedEnvironment:
    host: dict[str, str]
    collectors: tuple[CollectorOutput, ...]
    inventory: Inventory


def _run(runner: CommandRunner, argv: tuple[str, ...]) -> CommandResult:
    return runner.run(CommandRequest(argv))


def _isolated(
    identifier: str, empty: T, collector: Callable[[], tuple[CollectorOutput, T]]
) -> tuple[CollectorOutput, T]:
    try:
        return collector()
    except _TruncatedCommandError as exc:
        result = exc.result
        partial = CollectorOutput(
            identifier,
            CollectorStatus.PARTIAL,
            result.stdout + result.stderr,
            result.argv,
            result.exit_code,
            _error(result),
        )
        return partial, empty
    except (NativePlaneUnavailable, NativeAdapterError):
        raise
    except Exception as exc:  # noqa: BLE001 - WHY: one broken collector must not abort the remaining inventory.
        failed = CollectorOutput(
            identifier,
            CollectorStatus.FAILED,
            b"",
            (identifier,),
            None,
            f"{type(exc).__name__}: {exc}",
        )
        return failed, empty


def _text(result: CommandResult) -> str:
    return result.stdout.decode(errors="replace")


def _error(result: CommandResult) -> str | None:
    if result.timed_out:
        return "command timed out"
    if result.truncated:
        return _TRUNCATED_REASON
    return result.stderr.decode(errors="replace").strip() or None


def _which(runner: CommandRunner, name: str) -> str:
    found = _run(runner, ("/usr/bin/which", name))
    if found.truncated:
        raise _TruncatedCommandError(found)
    return _text(found).strip() if found.exit_code == 0 else ""


def _output(identifier: str, result: CommandResult) -> CollectorOutput:
    status = (
        CollectorStatus.PARTIAL
        if result.exit_code == 0 and not result.timed_out and result.truncated
        else CollectorStatus.OK
        if result.exit_code == 0 and not result.timed_out
        else CollectorStatus.FAILED
    )
    return CollectorOutput(
        identifier,
        status,
        result.stdout + result.stderr,
        result.argv,
        result.exit_code,
        _error(result),
    )


def _unavailable(identifier: str, tool: str) -> CollectorOutput:
    return CollectorOutput(
        identifier,
        CollectorStatus.UNAVAILABLE,
        b"",
        ("/usr/bin/which", tool),
        None,
        f"{tool} not found",
    )


def _brew(runner: CommandRunner) -> tuple[CollectorOutput, str]:
    executable = _which(runner, "brew")
    if not executable:
        return _unavailable("brew", "brew"), ""
    # ASSUMPTION: brew bundle dump accepts /dev/stdout as its Brewfile path.
    result = _run(
        runner, (executable, "bundle", "dump", "--file=/dev/stdout", "--force")
    )
    authoritative = result.exit_code == 0 and not result.truncated
    return _output("brew", result), _text(result) if authoritative else ""


def _mas(runner: CommandRunner) -> tuple[CollectorOutput, tuple[dict[str, str], ...]]:
    executable = _which(runner, "mas")
    if not executable:
        return _unavailable("mas", "mas"), ()
    result = _run(runner, (executable, "list"))
    parsed: list[dict[str, str]] = []
    malformed = False
    if result.exit_code == 0 and not result.truncated:
        # ASSUMPTION: mas list emits "<id> <name> (<version>)" per line.
        for line in _text(result).splitlines():
            app_id, separator, rest = line.partition(" ")
            name, version_separator, version = rest.rpartition(" (")
            if separator and version_separator and version.endswith(")"):
                parsed.append({"id": app_id, "name": name, "version": version[:-1]})
            else:
                malformed = True
    output = _output("mas", result)
    if malformed:
        output = CollectorOutput(
            output.id,
            CollectorStatus.PARTIAL,
            output.artifact,
            output.command,
            output.exit_code,
            "unrecognized mas output",
        )
    return output, tuple(parsed)


def _python(
    runner: CommandRunner,
) -> tuple[CollectorOutput, tuple[dict[str, str | list[str]], ...]]:
    uv = _which(runner, "uv")
    interpreters = tuple(
        dict.fromkeys(
            path for name in ("python3", "python") if (path := _which(runner, name))
        )
    )
    if not interpreters:
        return _unavailable("python", "python"), ()
    environments: list[dict[str, str | list[str]]] = []
    raw: list[dict[str, str | int | None]] = []
    fallback_used = False
    failed: list[CommandResult] = []
    truncated: CommandResult | None = None
    primary: tuple[str, ...] = (interpreters[0], "-m", "pip", "freeze")
    for executable in interpreters:
        version_result = _run(runner, (executable, "--version"))
        raw.append(
            {
                "command": " ".join(version_result.argv),
                "stdout": _text(version_result),
                "stderr": version_result.stderr.decode(errors="replace"),
                "exit_code": version_result.exit_code,
            }
        )
        command = (
            (uv, "pip", "freeze", "--python", executable)
            if uv
            else (executable, "-m", "pip", "freeze")
        )
        freeze = _run(runner, command)
        raw.append(
            {
                "command": " ".join(freeze.argv),
                "stdout": _text(freeze),
                "stderr": freeze.stderr.decode(errors="replace"),
                "exit_code": freeze.exit_code,
            }
        )
        primary = command
        if freeze.exit_code != 0 and uv:
            freeze = _run(runner, (executable, "-m", "pip", "freeze"))
            raw.append(
                {
                    "command": " ".join(freeze.argv),
                    "stdout": _text(freeze),
                    "stderr": freeze.stderr.decode(errors="replace"),
                    "exit_code": freeze.exit_code,
                }
            )
            fallback_used = True
        version = (
            _text(version_result) or version_result.stderr.decode(errors="replace")
        ).strip()
        requirements = [line for line in _text(freeze).splitlines() if line]
        if version_result.truncated or freeze.truncated:
            truncated = version_result if version_result.truncated else freeze
        elif freeze.exit_code == 0:
            environments.append(
                {
                    "scope": "system",
                    "executable": executable,
                    "version": version,
                    "requirements": requirements,
                }
            )
        else:
            failed.append(freeze)
    status = (
        CollectorStatus.PARTIAL
        if truncated is not None
        else CollectorStatus.FAILED
        if not environments
        else CollectorStatus.PARTIAL
        if failed or fallback_used
        else CollectorStatus.OK
    )
    artifact = (json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()
    detail = (
        _error(truncated)
        if truncated is not None
        else _error(failed[0])
        if failed
        else "uv failed; pip fallback used"
        if fallback_used
        else None
    )
    inventory = () if truncated is not None else tuple(environments)
    return CollectorOutput(
        "python", status, artifact, primary, 1 if failed else 0, detail
    ), inventory


def _npm(
    runner: CommandRunner,
) -> tuple[CollectorOutput, dict[str, str | list[dict[str, str]]]]:
    npm = _which(runner, "npm")
    empty: dict[str, str | list[dict[str, str]]] = {
        "node_version": "",
        "prefix": "",
        "packages": [],
    }
    if not npm:
        return _unavailable("npm", "npm"), empty
    node = _which(runner, "node")
    node_result = (
        _run(runner, (node, "--version"))
        if node
        else CommandResult(("/usr/bin/which", "node"), 1, b"", b"", False)
    )
    prefix = _run(runner, (npm, "prefix", "-g"))
    listing = _run(runner, (npm, "-g", "list", "--depth=0", "--json"))
    results = (node_result, prefix, listing)
    truncated = next((item for item in results if item.truncated), None)
    packages: list[dict[str, str]] = []
    parse_error: str | None = None
    try:
        # ASSUMPTION: npm depth-0 JSON stores package objects under dependencies.
        payload = (
            json.loads(_text(listing))
            if listing.exit_code == 0 and not listing.truncated
            else {}
        )
        dependencies = (
            payload.get("dependencies", {}) if isinstance(payload, dict) else {}
        )
        if isinstance(dependencies, dict):
            for name, detail in sorted(dependencies.items()):
                if isinstance(name, str) and isinstance(detail, dict):
                    version = detail.get("version", "")
                    packages.append(
                        {
                            "name": name,
                            "version": version if isinstance(version, str) else "",
                        }
                    )
    except json.JSONDecodeError as exc:
        parse_error = str(exc)
    status = (
        CollectorStatus.OK
        if all(item.exit_code == 0 for item in results)
        and parse_error is None
        and truncated is None
        else CollectorStatus.PARTIAL
    )
    artifact = b"".join(item.stdout + item.stderr for item in results)
    output = CollectorOutput(
        "npm",
        status,
        artifact,
        listing.argv,
        listing.exit_code,
        parse_error
        or (_error(truncated) if truncated is not None else _error(listing)),
    )
    if truncated is not None:
        return output, empty
    return output, {
        "node_version": _text(node_result).strip(),
        "prefix": _text(prefix).strip(),
        "packages": packages,
    }


def _pipx(runner: CommandRunner) -> tuple[CollectorOutput, tuple[dict[str, str], ...]]:
    executable = _which(runner, "pipx")
    if not executable:
        return _unavailable("pipx", "pipx"), ()
    result = _run(runner, (executable, "list", "--json"))
    artifact = result.stdout + result.stderr
    fallback = result.exit_code != 0
    if fallback:
        result = _run(runner, (executable, "list", "--short"))
        artifact += result.stdout + result.stderr
    packages: list[dict[str, str]] = []
    parse_error: str | None = None
    if not fallback and result.exit_code == 0 and not result.truncated:
        try:
            # ASSUMPTION: pipx JSON nests main package metadata under venvs.*.metadata.main_package.
            payload = json.loads(_text(result))
            venvs = payload.get("venvs", {}) if isinstance(payload, dict) else {}
            if isinstance(venvs, dict):
                for detail in venvs.values():
                    metadata = (
                        detail.get("metadata") if isinstance(detail, dict) else None
                    )
                    main = (
                        metadata.get("main_package")
                        if isinstance(metadata, dict)
                        else None
                    )
                    name = main.get("package") if isinstance(main, dict) else None
                    version = (
                        main.get("package_version") if isinstance(main, dict) else None
                    )
                    if isinstance(name, str) and isinstance(version, str):
                        packages.append({"name": name, "version": version})
                    else:
                        parse_error = "unrecognized pipx package metadata"
        except json.JSONDecodeError as exc:
            parse_error = str(exc)
    status = (
        CollectorStatus.PARTIAL
        if fallback or parse_error or result.truncated
        else CollectorStatus.OK
        if result.exit_code == 0
        else CollectorStatus.FAILED
    )
    return CollectorOutput(
        "pipx",
        status,
        artifact,
        result.argv,
        result.exit_code,
        parse_error or _error(result),
    ), tuple(packages)


def _applications(
    runner: CommandRunner,
) -> tuple[CollectorOutput, tuple[dict[str, str], ...]]:
    find = (
        "/usr/bin/find",
        "/Applications",
        "-maxdepth",
        "1",
        "-type",
        "d",
        "-name",
        "*.app",
        "-print",
    )
    result = _run(runner, find)
    applications: list[dict[str, str]] = []
    artifact = bytearray(result.stdout + result.stderr)
    partial = result.truncated
    truncated = result.truncated
    paths = (
        sorted(_text(result).splitlines())
        if result.exit_code == 0 and not result.truncated
        else ()
    )
    for path in paths:
        info = f"{path}/Contents/Info.plist"
        bundle = _run(
            runner,
            (
                "/usr/bin/plutil",
                "-extract",
                "CFBundleIdentifier",
                "raw",
                "-o",
                "-",
                info,
            ),
        )
        version = _run(
            runner,
            (
                "/usr/bin/plutil",
                "-extract",
                "CFBundleShortVersionString",
                "raw",
                "-o",
                "-",
                info,
            ),
        )
        partial = (
            partial
            or bundle.exit_code != 0
            or version.exit_code != 0
            or bundle.truncated
            or version.truncated
        )
        truncated = truncated or bundle.truncated or version.truncated
        artifact.extend(bundle.stdout + bundle.stderr + version.stdout + version.stderr)
        applications.append(
            {
                "name": path.rsplit("/", 1)[-1][:-4],
                "path": path,
                "bundle_id": _text(bundle).strip(),
                "version": _text(version).strip(),
            }
        )
    output = CollectorOutput(
        "applications",
        _output("applications", result).status,
        bytes(artifact),
        result.argv,
        result.exit_code,
        _error(result),
    )
    if partial:
        output = CollectorOutput(
            output.id,
            CollectorStatus.PARTIAL,
            output.artifact,
            output.command,
            output.exit_code,
            _TRUNCATED_REASON
            if truncated
            else "one or more app metadata fields unavailable",
        )
    inventory = () if truncated else tuple(applications)
    return output, inventory


def collect_environment(runner: CommandRunner) -> CollectedEnvironment:
    host_results = tuple(
        _run(runner, ("/usr/bin/uname", flag)) for flag in ("-s", "-r", "-m")
    )
    brew, brewfile = _isolated("brew", "", lambda: _brew(runner))
    python, python_inventory = _isolated("python", (), lambda: _python(runner))
    npm_empty: dict[str, str | list[dict[str, str]]] = {
        "node_version": "",
        "prefix": "",
        "packages": [],
    }
    npm, npm_inventory = _isolated("npm", npm_empty, lambda: _npm(runner))
    pipx, pipx_inventory = _isolated("pipx", (), lambda: _pipx(runner))
    mas, mas_inventory = _isolated("mas", (), lambda: _mas(runner))
    applications, app_inventory = _isolated(
        "applications", (), lambda: _applications(runner)
    )
    return CollectedEnvironment(
        {
            "os": _text(host_results[0]).strip(),
            "os_version": _text(host_results[1]).strip(),
            "arch": _text(host_results[2]).strip(),
        },
        (brew, mas, python, npm, pipx, applications),
        Inventory(
            brewfile,
            python_inventory,
            npm_inventory,
            pipx_inventory,
            mas_inventory,
            app_inventory,
        ),
    )
