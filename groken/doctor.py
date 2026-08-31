"""Tiered, secret-safe diagnostics for ``groken doctor``."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, cast

import anyio
import httpx

from .auth import TokenStateError, load_tokens
from .config import ConfigStateError, load_config
from .exec_service import ExecServiceClient
from .gateway import GatewayManager
from .local_health import (
    LocalCheck,
    inspect_environment,
    inspect_harnesses,
    inspect_native,
    inspect_routines,
)


class _DoctorGateway(Protocol):
    def ensure_sandbox_metadata(self) -> dict[str, object]: ...
    def command(self, method: str) -> object: ...


def _object_list(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return cast("list[object]", value)


def _say(label: str, message: str, *, warning: bool = False) -> None:
    print(f"{label}: {'WARN — ' if warning else ''}{message}")


def _expiry(tokens: dict[str, object]) -> str:
    value = tokens.get("expiresAt", tokens.get("expires_at", tokens.get("expiresIn")))
    if value is None:
        return "expiry unknown"
    try:
        if isinstance(value, str):
            when = datetime.fromisoformat(value).timestamp()
        elif isinstance(value, (int, float)):
            numeric_value = float(value)
            if numeric_value < 10_000_000_000:
                when = time.time() + numeric_value
            else:
                when = (
                    numeric_value / 1000
                    if numeric_value > 10_000_000_000_000
                    else numeric_value
                )
        else:
            return "expiry unknown"
        return f"expires in {max(0, int(when - time.time()))}s"
    except (TypeError, ValueError, OverflowError):
        return "expiry unknown"


async def _exec_daemon_check(manager: _DoctorGateway) -> bool:
    client = ExecServiceClient(manager)
    try:
        result = await client.execute("/usr/bin/true", timeout_ms=5_000)
        return result.exit_code == 0
    finally:
        await client.http.aclose()


def _exec_daemon_healthy(manager: _DoctorGateway) -> bool:
    return anyio.run(_exec_daemon_check, manager)


def _compatibility_tier() -> None:
    probes: tuple[tuple[str, Callable[[], LocalCheck]], ...] = (
        ("8a harnesses", inspect_harnesses),
        ("8b routines", inspect_routines),
        ("8c env", inspect_environment),
        ("8d native", inspect_native),
    )
    for label, probe in probes:
        try:
            check = probe()
            _say(label, check.message, warning=check.warning)
        except Exception:  # noqa: BLE001 - each diagnostic subcheck must name itself and continue.
            _say(label, "FAIL (local check unavailable)", warning=True)


def run_doctor() -> int:
    """Run diagnostics and return the documented process exit status."""
    local_state_ok = True
    try:
        _ = load_config()
    except ConfigStateError as exc:
        local_state_ok = False
        _say("0 config", str(exc), warning=True)

    try:
        tokens = load_tokens()
    except TokenStateError as exc:
        local_state_ok = False
        tokens = None
        _say("1 tokens", str(exc), warning=True)
    token_ok = bool(tokens and tokens.get("accessToken"))
    if token_ok:
        _say("1 tokens", f"present ({_expiry(tokens or {})})")
    elif tokens is not None or local_state_ok:
        _say("1 tokens", "MISSING — run: groken login", warning=True)

    metadata: dict[str, object] = {}
    gateway_ok = False
    manager: _DoctorGateway | None = None
    try:
        manager = GatewayManager()
        metadata = manager.ensure_sandbox_metadata()
        agents = _object_list(manager.command("listAgents"))
        if agents is None:
            raise TypeError("gateway agent list must be an array")
        _say("2 gateway", f"ok ({len(agents)} agents)")
        gateway_ok = True
    except Exception:  # noqa: BLE001 - diagnostic must continue through soft tiers
        _say("2 gateway", "FAIL (gateway unavailable)", warning=True)

    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get("http://127.0.0.1:18766/healthz")
            _ = response.raise_for_status()
        _say("3 controller", "healthz ok")
    except Exception:  # noqa: BLE001
        _say("3 controller", "down (skipped)", warning=True)

    _say("4 model", "controller-owned (not independently probed)")

    if manager is not None and metadata.get("execDaemonUrl"):
        try:
            if not _exec_daemon_healthy(manager):
                raise RuntimeError("exec command failed")
            _say("5 execDaemon", "command ok")
        except Exception:  # noqa: BLE001
            _say("5 execDaemon", "command failed", warning=True)
    else:
        _say("5 execDaemon", "metadata unavailable", warning=True)

    current = metadata.get("podId")
    _say(
        "6 podId",
        "metadata available" if current else "metadata unavailable",
        warning=current is None,
    )

    try:
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "groken-doctor", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        result = subprocess.run(
            [sys.executable, "-m", "groken.mcp_server"],
            input=request,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            _say("7 MCP", "self-handshake ok")
        else:
            raise RuntimeError("no valid response")
    except Exception:  # noqa: BLE001
        _say("7 MCP", "self-handshake failed", warning=True)

    _compatibility_tier()
    return 0 if local_state_ok and token_ok and gateway_ok else 1
