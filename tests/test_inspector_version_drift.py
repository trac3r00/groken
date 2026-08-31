import json
import plistlib
import struct
from pathlib import Path
from typing import cast

import pytest

from groken import cli
from groken.capabilities import CURRENT_027_COMMAND_NAMES, CURRENT_030_COMMAND_NAMES
from groken.gateway_versions import CURRENT_027_NO_ARGS, CURRENT_030_NO_ARGS
from groken.inspect_app import HOST_MAIN, inspect_app

JsonObject = dict[str, object]
_CRITICAL = {
    "createAgent": "createAgent:E().args(P_)",
    "duplicateAgent": "duplicateAgent:E().args(me)",
    "getForeverBoxStatus": "getForeverBoxStatus:E().args(me)",
    "updateAgent": "updateAgent:E().args({id:f(),profile:O_})",
    "updateForeverBox": "updateForeverBox:E().args(me)",
    "updateHostNow": (
        "updateHostNow:E().args({force:b(H()),includeErrorDetail:b(H())})"
    ),
}
_CRITICAL_030 = {
    "createAgent": "createAgent:_().args(UA)",
    "duplicateAgent": "duplicateAgent:_().args(Ee)",
    "getForeverBoxStatus": "getForeverBoxStatus:_().args(Ee)",
    "updateAgent": "updateAgent:_().args({id:g(),profile:MA})",
    "updateForeverBox": "updateForeverBox:_().args(Ee)",
    "updateHostNow": (
        "updateHostNow:_().args({force:v(Z()),includeErrorDetail:v(Z())})"
    ),
}


def object_dict(value: object) -> JsonObject:
    assert isinstance(value, dict)
    return cast("JsonObject", value)


def write_asar(path: Path, files: dict[str, bytes]) -> None:
    tree: JsonObject = {"files": {}}
    blobs: list[bytes] = []
    offset = 0
    for name, blob in files.items():
        node = tree
        parts = name.split("/")
        for part in parts[:-1]:
            children = object_dict(node["files"])
            node = object_dict(children.setdefault(part, {"files": {}}))
        object_dict(node["files"])[parts[-1]] = {
            "size": len(blob),
            "offset": str(offset),
        }
        blobs.append(blob)
        offset += len(blob)
    payload = json.dumps(tree).encode()
    padding = (4 - len(payload) % 4) % 4
    string_size = len(payload) + 4 + padding
    _ = path.write_bytes(
        struct.pack("<4I", 4, string_size + 4, string_size, len(payload))
        + payload
        + b"\0" * padding
        + b"".join(blobs)
    )


def current_source(*, changed_update: bool = False) -> bytes:
    entries: list[str] = []
    for name in CURRENT_027_COMMAND_NAMES:
        critical = _CRITICAL.get(name)
        if critical is not None:
            entries.append(critical)
        elif name in CURRENT_027_NO_ARGS:
            entries.append(f"{name}:E().noArgs")
        else:
            entries.append(f"{name}:E().args({{}})")
    source = "var table={" + ",".join(entries) + "};"
    if changed_update:
        source = source.replace(
            "updateForeverBox:E().args(me)",
            "updateForeverBox:E().args({id:f(),force:b(H())})",
        )
    return source.encode()


def make_app(root: Path, *, version: str | None, changed_update: bool = False) -> Path:
    app = root / "Grok Bot.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    if version is not None:
        _ = (app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleShortVersionString": version})
        )
    package = {"name": "sand"}
    if version is not None:
        package["version"] = version
    write_asar(
        resources / "app.asar",
        {
            "package.json": json.dumps(package).encode(),
            HOST_MAIN: current_source(changed_update=changed_update),
        },
    )
    return app


def current_030_source(*, changed_update: bool = False) -> bytes:
    entries: list[str] = []
    for name in CURRENT_030_COMMAND_NAMES:
        critical = _CRITICAL_030.get(name)
        if critical is not None:
            entries.append(critical)
        elif name in CURRENT_030_NO_ARGS:
            entries.append(f"{name}:_().noArgs")
        else:
            entries.append(f"{name}:_().args({{}})")
    source = "var table={" + ",".join(entries) + "};"
    if changed_update:
        source = source.replace(
            "updateForeverBox:_().args(Ee)",
            "updateForeverBox:_().args({id:g(),force:Z()})",
        )
    return source.encode()


def make_030_app(root: Path, *, changed_update: bool = False) -> Path:
    app = root / "Grok Bot.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    _ = (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "0.30.0"})
    )
    write_asar(
        resources / "app.asar",
        {
            "package.json": b'{"name":"sand","version":"0.30.0"}',
            HOST_MAIN: current_030_source(changed_update=changed_update),
        },
    )
    return app


def test_hash_mismatch_with_matching_critical_fingerprints_stays_partial_clean(
    tmp_path: Path,
) -> None:
    # Given
    app = make_app(tmp_path, version="0.27.0")

    # When
    report = inspect_app(app)

    # Then
    assert report["reference_hash_match"] is False
    assert report["drift"]["changed"] == []
    assert report["drift"]["unknown"] == []
    assert len(report["drift"]["unchanged"]) == 143
    assert report["drift"]["schemas_verified"] is False
    assert report["drift"]["clean"] is True


def test_changed_critical_validator_is_named_and_fails_on_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    app = make_app(tmp_path, version="0.27.0", changed_update=True)

    # When / Then
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_inspect_app(str(app), True)
    assert excinfo.value.code == 2
    report = object_dict(cast("object", json.loads(capsys.readouterr().out)))
    drift = object_dict(report["drift"])
    assert drift["changed"] == ["updateForeverBox"]
    assert drift["unknown"] == []
    assert drift["clean"] is False


def test_030_profile_uses_its_own_critical_fingerprints(tmp_path: Path) -> None:
    clean_report = inspect_app(make_030_app(tmp_path / "clean"))
    changed_report = inspect_app(
        make_030_app(tmp_path / "changed", changed_update=True)
    )

    assert clean_report["expected_profile"] == "grok-bot-0.30"
    assert clean_report["drift"]["changed"] == []
    assert clean_report["drift"]["unknown"] == []
    assert len(clean_report["drift"]["unchanged"]) == 147
    assert clean_report["drift"]["clean"] is True
    assert changed_report["drift"]["changed"] == ["updateForeverBox"]
    assert changed_report["drift"]["clean"] is False


def test_missing_versions_never_select_legacy_profile(tmp_path: Path) -> None:
    # Given
    app = make_app(tmp_path, version=None)

    # When
    report = inspect_app(app)

    # Then
    assert report["expected_profile"] == "unknown"
    assert report["drift"]["clean"] is False
