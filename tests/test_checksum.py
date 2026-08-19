import subprocess

from groken import checksum


def _force_uuid_fallback(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("no ioreg")

    monkeypatch.setattr(subprocess, "run", boom)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(checksum, "_STATE_DIR", state_dir)
    monkeypatch.setattr(checksum, "_MACHINE_ID_FILE", state_dir / "machine_id")
    return state_dir / "machine_id"


def test_machine_id_generates_and_persists(monkeypatch, tmp_path):
    mid_file = _force_uuid_fallback(monkeypatch, tmp_path)
    first = checksum.get_machine_id()
    assert mid_file.read_text() == first
    assert (mid_file.stat().st_mode & 0o777) == 0o600
    assert checksum.get_machine_id() == first


def test_machine_id_reads_existing_file(monkeypatch, tmp_path):
    mid_file = _force_uuid_fallback(monkeypatch, tmp_path)
    mid_file.parent.mkdir(parents=True)
    mid_file.write_text("stored-id\n")
    assert checksum.get_machine_id() == "stored-id"
