from groken.checksum import create_cursor_checksum


def js_reference_checksum(machine_id: str, unix_kilo_seconds: int) -> str:
    import base64
    last = 165
    out = bytearray(6)
    raw = unix_kilo_seconds.to_bytes(6, "big")
    for i, b in enumerate(raw):
        out[i] = ((b ^ last) + i) % 256
        last = out[i]
    return base64.urlsafe_b64encode(bytes(out)).decode().rstrip("=") + machine_id


def test_checksum_matches_reference():
    for ts in (0, 1, 1787115305, 9999999999, 123456789012):
        got = create_cursor_checksum("MACHINE123", now_ms=ts * 1_000_000 + 999)
        assert got == js_reference_checksum("MACHINE123", ts), (ts, got)

