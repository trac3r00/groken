"""Port of the app's x-cursor-checksum (reversed from bundle). JS stores `(b ^ last) + i`
into a Uint8Array, so each byte wraps mod 256; we apply & 0xFF explicitly.
"""

from __future__ import annotations

import base64
import subprocess
import time
import uuid as uuidlib
from pathlib import Path

_STATE_DIR = Path.home() / ".config" / "groken"
_MACHINE_ID_FILE = _STATE_DIR / "machine_id"


def _enhanced_obfuscate(data: bytes) -> bytes:
    out = bytearray(len(data))
    last = 165
    for i, b in enumerate(data):
        out[i] = ((b ^ last) + i) & 0xFF
        last = out[i]
    return bytes(out)


def create_cursor_checksum(machine_id: str, now_ms: float | None = None) -> str:
    unix_kilo_seconds = int((now_ms if now_ms is not None else time.time() * 1000) // 1_000_000)
    raw = unix_kilo_seconds.to_bytes(6, "big")
    obfuscated = _enhanced_obfuscate(raw)
    checksum = base64.urlsafe_b64encode(obfuscated).decode().rstrip("=")
    return f"{checksum}{machine_id}"


def get_machine_id() -> str:
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                return line.split('"')[-2].strip()
    except Exception:
        pass
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    if _MACHINE_ID_FILE.exists():
        return _MACHINE_ID_FILE.read_text().strip()
    mid = str(uuidlib.uuid4())
    _MACHINE_ID_FILE.write_text(mid)
    _MACHINE_ID_FILE.chmod(0o600)
    return mid
