"""Tiered, secret-safe diagnostics for ``groken doctor``."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .auth import load_tokens
from .config import load_config
from .gateway import GatewayManager


def _say(label: str, message: str, *, warning: bool = False) -> None:
    print(f"{label}: {'WARN — ' if warning else ''}{message}")


def _expiry(tokens: dict[str, Any]) -> str:
    value = tokens.get("expiresAt", tokens.get("expires_at", tokens.get("expiresIn")))
    if value is None:
        return "expiry unknown"
    try:
        if isinstance(value, str):
            when = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        elif float(value) < 10_000_000_000:
            when = time.time() + float(value)
        else:
            when = float(value) / 1000 if float(value) > 10_000_000_000_000 else float(value)
        return f"expires in {max(0, int(when - time.time()))}s"
    except (TypeError, ValueError, OverflowError):
        return "expiry unknown"


def run_doctor() -> int:
    """Run diagnostics and return the documented process exit status."""
    tokens = load_tokens()
    token_ok = bool(tokens and tokens.get("accessToken"))
    if token_ok:
        _say("1 tokens", f"present ({_expiry(tokens or {})})")
    else:
        _say("1 tokens", "MISSING — run: groken login", warning=True)

    metadata: dict[str, Any] = {}
    gateway_ok = False
    try:
        manager = GatewayManager()
        metadata = manager.ensure_sandbox_metadata()
        agents = manager.command("listAgents")
        _say("2 gateway", f"ok ({len(agents)} agents)")
        gateway_ok = True
    except Exception as exc:  # diagnostic must continue through soft tiers
        _say("2 gateway", "FAIL (gateway unavailable)", warning=True)

    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get("http://127.0.0.1:18766/healthz")
            response.raise_for_status()
        _say("3 controller", "healthz ok")
    except Exception:
        _say("3 controller", "down (skipped)", warning=True)

    cfg = load_config()
    model_url = cfg.get("model_base_url") or cfg.get("modelBaseUrl")
    model_key = cfg.get("model_api_key") or cfg.get("modelApiKey")
    if model_url:
        try:
            headers = {"authorization": f"Bearer {model_key}"} if model_key else {}
            with httpx.Client(timeout=5.0) as client:
                response = client.get(str(model_url).rstrip("/") + "/models", headers=headers)
                response.raise_for_status()
            _say("4 model", "authenticated ping ok")
        except Exception:
            _say("4 model", "authenticated ping failed", warning=True)
    else:
        _say("4 model", "not configured", warning=True)

    if metadata.get("execDaemonUrl"):
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.head(str(metadata["execDaemonUrl"]), headers={"authorization": "Bearer [redacted]"})
                response.raise_for_status()
            _say("5 execDaemon", "HEAD ok")
        except Exception:
            _say("5 execDaemon", "HEAD failed", warning=True)
    else:
        _say("5 execDaemon", "metadata unavailable", warning=True)

    cached = cfg.get("podId") or cfg.get("pod_id")
    current = metadata.get("podId")
    if cached and current and str(cached) != str(current):
        _say("6 podId", "ALARM — changed since cached config", warning=True)
    else:
        _say("6 podId", "unchanged" if cached and current else "no cached podId")

    try:
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        result = subprocess.run(["groken-mcp", "initialize"], input=request, text=True,
                                capture_output=True, timeout=5, check=False)
        if result.returncode == 0 and result.stdout.strip():
            _say("7 MCP", "self-handshake ok")
        else:
            raise RuntimeError("no valid response")
    except Exception:
        _say("7 MCP", "self-handshake failed", warning=True)
    return 0 if token_ok and gateway_ok else 1
