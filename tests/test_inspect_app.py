import hashlib
import json
import plistlib
import struct
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from groken.capabilities import GATEWAY_COMMANDS
from groken.inspect_app import (
    HOST_MAIN,
    AsarError,
    diff_commands,
    extract_command_names,
    extract_service_methods,
    inspect_app,
    read_asar_entry,
    read_asar_header,
)

EXPECTED = [spec.name for spec in GATEWAY_COMMANDS]
JsonObject = dict[str, object]


def object_value(value: object) -> JsonObject:
    assert isinstance(value, dict)
    return cast("JsonObject", value)


def run_cli_main() -> None:
    from groken import cli

    main_impl_name = "_main_impl"
    main_impl = cast("Callable[[], None]", getattr(cli, main_impl_name))
    main_impl()


def build_asar(path: Path, files: dict[str, bytes]) -> Path:
    """Write a minimal but format-valid asar archive containing ``files``."""
    tree: JsonObject = {"files": {}}
    blobs: list[bytes] = []
    offset = 0
    for name, blob in files.items():
        node = tree
        parts = name.split("/")
        for part in parts[:-1]:
            children = object_value(node["files"])
            node = object_value(children.setdefault(part, {"files": {}}))
        children = object_value(node["files"])
        children[parts[-1]] = {"size": len(blob), "offset": str(offset)}
        blobs.append(blob)
        offset += len(blob)

    payload = json.dumps(tree).encode()
    json_len = len(payload)
    padding = (4 - json_len % 4) % 4
    str_size = json_len + 4 + padding
    header_size = str_size + 4
    head = struct.pack("<4I", 4, header_size, str_size, json_len)
    _ = path.write_bytes(head + payload + b"\0" * padding + b"".join(blobs))
    return path


def host_source(commands: list[str], *, services: str = "") -> bytes:
    args_by_name = {spec.name: spec.args for spec in GATEWAY_COMMANDS}
    entries = ",".join(
        f"{name}:t=>t.{name}()"
        if args_by_name.get(name) == "none"
        else f"{name}:(t,e)=>t.{name}(Tt(e))"
        for name in commands
    )
    return (f"var v1={{{entries}}};{services}").encode()


SERVICE_BLOCK = (
    'var S={typeName:"agent.v1.AgentService",methods:{'
    'run:{name:"Run",I:a,O:b,kind:x.BiDiStreaming},'
    'nameAgent:{name:"NameAgent",I:c,O:d,kind:x.Unary}}};'
    'var E={typeName:"agent.v1.ExecService",methods:{'
    'exec:{name:"Exec",I:e,O:f,kind:x.ServerStreaming}}};'
)


def make_app(root: Path, commands: list[str], *, version: str = "0.24.0") -> Path:
    app = root / "Grok Bot.app"
    contents = app / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    _ = (contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "0.27.0"})
    )
    _ = build_asar(
        resources / "app.asar",
        {
            "package.json": json.dumps({"name": "sand", "version": version}).encode(),
            HOST_MAIN: host_source(commands, services=SERVICE_BLOCK),
        },
    )
    return app


def test_header_and_entry_roundtrip(tmp_path: Path) -> None:
    archive = build_asar(
        tmp_path / "a.asar", {"dist/host/host-main.cjs": b"hello world"}
    )

    header = read_asar_header(archive)

    assert header.data_offset > 0
    assert read_asar_entry(archive, header, "dist/host/host-main.cjs") == b"hello world"


def test_missing_entry_fails_closed(tmp_path: Path) -> None:
    archive = build_asar(tmp_path / "a.asar", {"dist/host/host-main.cjs": b"x"})
    header = read_asar_header(archive)

    with pytest.raises(AsarError):
        _ = read_asar_entry(archive, header, "dist/host/nope.cjs")


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\x01\x00\x00\x00",
        b"\x09\x00\x00\x00" + b"\x10\x00\x00\x00" * 3,  # bad magic word
        struct.pack("<4I", 4, 64, 60, 56),  # truncated: header bytes absent
        struct.pack("<4I", 4, 20, 16, 12) + b"not-json----",
        struct.pack("<4I", 4, 16, 12, 8) + b'["list"]',  # header json is not an object
    ],
)
def test_corrupt_headers_fail_closed(tmp_path: Path, blob: bytes) -> None:
    archive = tmp_path / "corrupt.asar"
    _ = archive.write_bytes(blob)

    with pytest.raises(AsarError):
        _ = read_asar_header(archive)


def test_truncated_file_body_fails_closed(tmp_path: Path) -> None:
    archive = build_asar(
        tmp_path / "a.asar", {"dist/host/host-main.cjs": b"0123456789"}
    )
    header = read_asar_header(archive)
    data = archive.read_bytes()
    _ = archive.write_bytes(data[:-4])

    with pytest.raises(AsarError):
        _ = read_asar_entry(archive, header, "dist/host/host-main.cjs")


def test_extract_commands_picks_dispatch_table() -> None:
    source = host_source(EXPECTED, services=SERVICE_BLOCK).decode()

    names = extract_command_names(source)

    assert list(names) == sorted(EXPECTED)


def test_extract_commands_ignores_small_unrelated_tables() -> None:
    noise = "var q={isSpotlightEnabled:t=>t.isSpotlightEnabled(),debug:t=>t.debug()};"
    source = noise + host_source(EXPECTED).decode()

    assert list(extract_command_names(source)) == sorted(EXPECTED)


def test_extract_service_methods() -> None:
    methods = extract_service_methods(SERVICE_BLOCK)

    assert methods == {
        "agent.v1.AgentService": ["NameAgent", "Run"],
        "agent.v1.ExecService": ["Exec"],
    }


def test_diff_reports_added_removed_and_renamed() -> None:
    expected = ["listAgents", "countAgents", "submitSecret", "dismissWidget"]
    found = ["listAgents", "countAgents", "submitSecretV2", "brandNewCommand"]

    drift = diff_commands(found=found, expected=expected)

    assert drift["removed"] == ["dismissWidget"]
    assert drift["added"] == ["brandNewCommand"]
    assert drift["renamed"] == [{"from": "submitSecret", "to": "submitSecretV2"}]
    assert drift["clean"] is False


def test_diff_clean_when_identical() -> None:
    drift = diff_commands(found=EXPECTED, expected=EXPECTED)

    assert drift == {"added": [], "removed": [], "renamed": [], "clean": True}


def test_inspect_app_no_drift(tmp_path: Path) -> None:
    app = make_app(tmp_path, EXPECTED)

    report = inspect_app(app)

    archive = app / "Contents" / "Resources" / "app.asar"
    host = host_source(EXPECTED, services=SERVICE_BLOCK)
    assert report["bundle_version"] == "0.27.0"
    assert report["embedded_package_version"] == "0.24.0"
    assert report["app_version"] == "0.24.0"
    assert report["asar_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert report["host_main_sha256"] == hashlib.sha256(host).hexdigest()
    assert report["command_count"] == len(EXPECTED)
    drift = object_value(report["drift"])
    assert drift["added"] == []
    assert drift["removed"] == []
    assert drift["changed"] == []
    assert drift["names_verified"] is True
    assert drift["schemas_verified"] is False
    assert drift["clean"] is True
    assert report["warnings"]
    services = object_value(report["services"])
    assert services["agent.v1.ExecService"] == ["Exec"]
    assert report["host_main"] == HOST_MAIN


def test_inspect_app_detects_drift(tmp_path: Path) -> None:
    mutated = [n for n in EXPECTED if n != "clearTrays"]
    mutated[mutated.index("dismissTray")] = "dismissTrayItem"
    mutated.append("teleportAgent")
    app = make_app(tmp_path, mutated, version="0.24.0")

    report = inspect_app(app)

    assert report["app_version"] == "0.24.0"
    drift = object_value(report["drift"])
    assert drift["clean"] is False
    assert drift["removed"] == ["clearTrays"]
    assert drift["added"] == ["teleportAgent"]
    assert drift["renamed"] == [{"from": "dismissTray", "to": "dismissTrayItem"}]
    assert json.dumps(report)  # report stays JSON-serializable


def test_inspect_app_missing_bundle_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AsarError):
        _ = inspect_app(tmp_path / "Absent.app")


def test_cli_inspect_app_prints_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(tmp_path, EXPECTED)
    monkeypatch.setattr("sys.argv", ["groken", "inspect-app", "--app-path", str(app)])

    run_cli_main()

    loaded = cast("object", json.loads(capsys.readouterr().out))
    payload = object_value(loaded)
    drift = object_value(payload["drift"])
    assert drift["clean"] is True


def test_cli_inspect_app_fail_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = make_app(tmp_path, [n for n in EXPECTED if n != "clearTrays"])
    monkeypatch.setattr(
        "sys.argv",
        ["groken", "inspect-app", "--app-path", str(app), "--fail-on-drift"],
    )

    with pytest.raises(SystemExit) as excinfo:
        run_cli_main()

    assert excinfo.value.code == 2
    payload = object_value(cast("object", json.loads(capsys.readouterr().out)))
    drift = object_value(payload["drift"])
    assert drift["removed"] == ["clearTrays"]


def test_cli_fail_on_drift_names_same_name_handler_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = make_app(tmp_path, EXPECTED)
    source = host_source(EXPECTED, services=SERVICE_BLOCK).replace(
        b"listAgents:t=>t.listAgents()", b"listAgents:t=>t.countAgents()"
    )
    _ = build_asar(
        app / "Contents" / "Resources" / "app.asar",
        {
            "package.json": b'{"name":"sand","version":"0.24.0"}',
            HOST_MAIN: source,
        },
    )
    monkeypatch.setattr(
        "sys.argv", ["groken", "inspect-app", "--app-path", str(app), "--fail-on-drift"]
    )

    with pytest.raises(SystemExit) as excinfo:
        run_cli_main()

    assert excinfo.value.code == 2
    payload = object_value(cast("object", json.loads(capsys.readouterr().out)))
    assert object_value(payload["drift"])["changed"] == ["listAgents"]


def test_cli_inspect_app_reports_corrupt_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "Broken.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    _ = (resources / "app.asar").write_bytes(b"\x99\x00\x00\x00garbage")
    monkeypatch.setattr("sys.argv", ["groken", "inspect-app", "--app-path", str(app)])

    with pytest.raises(SystemExit) as excinfo:
        run_cli_main()

    assert excinfo.value.code != 0
