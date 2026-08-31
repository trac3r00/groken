from __future__ import annotations

import hashlib
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from groken import cli, env_manifest
from groken.env_collectors import (
    CommandRequest,
    CommandResult,
    Inventory,
    ManifestTree,
    NativePlaneUnavailable,
    collect_environment,
)
from groken.env_manifest import (
    BotIdentity,
    CaptureConfig,
    CaptureError,
    CaptureOutcome,
    CaptureSource,
    ChatCollector,
    capture_environment,
)


class ScriptedRunner:
    def __init__(
        self, pod: Path, replies: dict[tuple[str, ...], CommandResult]
    ) -> None:
        self.pod = pod
        self.replies = replies
        self.requests: list[CommandRequest] = []
        self.published: ManifestTree | None = None
        self.error: Exception | None = None
        self.failures: dict[tuple[str, ...], Exception] = {}

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        failure = self.failures.get(request.argv)
        if failure is not None:
            raise failure
        return self.replies.get(request.argv, response(request.argv, "", 1))

    def publish(self, tree: ManifestTree) -> None:
        if self.error is not None:
            raise self.error
        self.published = tree
        target = self.pod / tree.manifest_id
        for item in tree.files:
            path = target / item.path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for directory in (self.pod, target, path.parent):
                directory.chmod(0o700)
            path.write_bytes(item.content)
            path.chmod(0o600)


class ScriptedChat(ChatCollector):
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[BotIdentity] = []

    def collect(self, bot: BotIdentity) -> str:
        self.calls.append(bot)
        return self.raw


def response(argv: tuple[str, ...], stdout: str, code: int = 0) -> CommandResult:
    return CommandResult(
        argv, code, stdout.encode(), b"" if code == 0 else b"failed", False
    )


def fixture_replies() -> dict[tuple[str, ...], CommandResult]:
    app = "/Applications/Demo App.app"
    values = {
        ("/usr/bin/uname", "-s"): "Darwin\n",
        ("/usr/bin/uname", "-r"): "25.5.0\n",
        ("/usr/bin/uname", "-m"): "arm64\n",
        ("/usr/bin/which", "brew"): "/opt/homebrew/bin/brew\n",
        ("/usr/bin/which", "mas"): "/opt/homebrew/bin/mas\n",
        ("/usr/bin/which", "uv"): "/opt/homebrew/bin/uv\n",
        ("/usr/bin/which", "python3"): "/usr/bin/python3\n",
        ("/usr/bin/which", "python"): "",
        ("/usr/bin/which", "npm"): "/usr/local/bin/npm\n",
        ("/usr/bin/which", "node"): "/usr/local/bin/node\n",
        ("/usr/bin/which", "pipx"): "/opt/homebrew/bin/pipx\n",
        (
            "/opt/homebrew/bin/brew",
            "bundle",
            "dump",
            "--file=/dev/stdout",
            "--force",
        ): 'brew "jq"\n',
        ("/opt/homebrew/bin/mas", "list"): "497799835 Xcode (16.4)\n",
        ("/usr/bin/python3", "--version"): "Python 3.13.5\n",
        (
            "/opt/homebrew/bin/uv",
            "pip",
            "freeze",
            "--python",
            "/usr/bin/python3",
        ): "httpx==0.28.1\n",
        ("/usr/local/bin/node", "--version"): "v24.5.0\n",
        ("/usr/local/bin/npm", "prefix", "-g"): "/usr/local\n",
        (
            "/usr/local/bin/npm",
            "-g",
            "list",
            "--depth=0",
            "--json",
        ): '{"dependencies":{"typescript":{"version":"5.9.2"}}}\n',
        (
            "/opt/homebrew/bin/pipx",
            "list",
            "--json",
        ): '{"venvs":{"ruff":{"metadata":{"main_package":{"package":"ruff","package_version":"0.12.9"}}}}}\n',
        (
            "/usr/bin/find",
            "/Applications",
            "-maxdepth",
            "1",
            "-type",
            "d",
            "-name",
            "*.app",
            "-print",
        ): f"{app}\n",
        (
            "/usr/bin/plutil",
            "-extract",
            "CFBundleIdentifier",
            "raw",
            "-o",
            "-",
            f"{app}/Contents/Info.plist",
        ): "com.demo.app\n",
        (
            "/usr/bin/plutil",
            "-extract",
            "CFBundleShortVersionString",
            "raw",
            "-o",
            "-",
            f"{app}/Contents/Info.plist",
        ): "1.2.3\n",
    }
    return {argv: response(argv, value) for argv, value in values.items()}


def chat_payload() -> dict[str, object]:
    return {
        "host": {"os": "Darwin", "os_version": "25.5.0", "arch": "arm64"},
        "inventory": {
            "brewfile": "",
            "python": [],
            "npm": {"node_version": "", "prefix": "", "packages": []},
            "pipx": [],
            "mas": [],
            "applications": [],
        },
    }


def config(root: Path, bot_id: str = "bot-1") -> CaptureConfig:
    return CaptureConfig(
        bot=BotIdentity(bot_id, "Demo"),
        local_root=root / "local",
        captured_at=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )


def files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def load_manifest(outcome: CaptureOutcome) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((outcome.local_path / "manifest.json").read_text()),
    )


def recompute_id(outcome: CaptureOutcome) -> str:
    payload = load_manifest(outcome)
    _ = payload.pop("manifest_id")
    canonical = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    digest = hashlib.sha256(canonical)
    for path in sorted((outcome.local_path / "artifacts").iterdir()):
        relative = str(path.relative_to(outcome.local_path)).encode()
        content = path.read_bytes()
        digest.update(
            len(relative).to_bytes(8, "big")
            + relative
            + len(content).to_bytes(8, "big")
            + content
        )
    return f"sha256:{digest.hexdigest()}"


def test_env_capture_full_fixture_has_exact_schema_deterministic_id_and_mirror(
    tmp_path: Path,
) -> None:
    # Given
    first = ScriptedRunner(tmp_path / "pod-a", fixture_replies())
    second = ScriptedRunner(tmp_path / "pod-b", fixture_replies())

    # When
    one = capture_environment(config(tmp_path / "a"), first)
    two = capture_environment(config(tmp_path / "b"), second)

    # Then
    payload = load_manifest(one)
    assert set(payload) == {
        "schema_version",
        "manifest_id",
        "bot",
        "captured_at",
        "host",
        "collectors",
        "inventory",
    }
    assert set(cast("dict[str, object]", payload["bot"])) == {"id", "name"}
    assert set(cast("dict[str, object]", payload["host"])) == {
        "os",
        "os_version",
        "arch",
    }
    rows = cast("list[dict[str, object]]", payload["collectors"])
    assert all(
        set(row)
        == {"id", "status", "artifact", "sha256", "command", "exit_code", "error"}
        for row in rows
    )
    inventory = cast("dict[str, object]", payload["inventory"])
    assert set(inventory) == {
        "brewfile",
        "python",
        "npm",
        "pipx",
        "mas",
        "applications",
    }
    assert inventory["brewfile"] == 'brew "jq"\n'
    assert cast("dict[str, object]", inventory["npm"])["packages"] == [
        {"name": "typescript", "version": "5.9.2"}
    ]
    assert one.manifest_id == two.manifest_id
    assert one.manifest_id == recompute_id(one)
    assert one.source is CaptureSource.NATIVE
    assert first.published is not None
    assert files(one.local_path) == files(first.pod / one.manifest_id)
    assert (
        json.loads((one.local_path.parent / "current.json").read_text())["manifest_id"]
        == one.manifest_id
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == (0o600 if path.is_file() else 0o700)
        for path in one.local_path.parent.rglob("*")
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == (0o600 if path.is_file() else 0o700)
        for path in (first.pod / one.manifest_id).rglob("*")
    )
    forbidden = {"install", "upgrade", "remove", "uninstall"}
    assert not any(forbidden.intersection(request.argv) for request in first.requests)


def test_env_capture_failed_collector_survives_and_does_not_trigger_chat(
    tmp_path: Path,
) -> None:
    # Given
    replies = fixture_replies()
    brew = ("/opt/homebrew/bin/brew", "bundle", "dump", "--file=/dev/stdout", "--force")
    replies[brew] = response(brew, "", 2)
    runner = ScriptedRunner(tmp_path / "pod", replies)
    chat = ScriptedChat("not used")

    # When
    outcome = capture_environment(config(tmp_path), runner, chat)

    # Then
    rows = cast("list[dict[str, object]]", load_manifest(outcome)["collectors"])
    assert next(row for row in rows if row["id"] == "brew")["status"] == "failed"
    assert next(row for row in rows if row["id"] == "npm")["status"] == "ok"
    assert chat.calls == []


def test_env_capture_truncated_collectors_are_partial_and_non_authoritative(
    tmp_path: Path,
) -> None:
    # Given
    replies = fixture_replies()
    commands = (
        ("/opt/homebrew/bin/brew", "bundle", "dump", "--file=/dev/stdout", "--force"),
        ("/opt/homebrew/bin/mas", "list"),
        ("/opt/homebrew/bin/uv", "pip", "freeze", "--python", "/usr/bin/python3"),
        ("/usr/local/bin/npm", "-g", "list", "--depth=0", "--json"),
        ("/opt/homebrew/bin/pipx", "list", "--json"),
        (
            "/usr/bin/find",
            "/Applications",
            "-maxdepth",
            "1",
            "-type",
            "d",
            "-name",
            "*.app",
            "-print",
        ),
    )
    for command in commands:
        result = replies[command]
        replies[command] = CommandResult(
            result.argv,
            result.exit_code,
            result.stdout,
            result.stderr,
            result.timed_out,
            True,
        )
    runner = ScriptedRunner(tmp_path / "pod", replies)

    # When
    collected = collect_environment(runner)

    # Then
    assert all(row.status.value == "partial" for row in collected.collectors)
    assert all("truncated" in str(row.error) for row in collected.collectors)
    assert collected.inventory == Inventory(
        "",
        (),
        {"node_version": "", "prefix": "", "packages": []},
        (),
        (),
        (),
    )


def test_truncated_availability_probe_is_partial_not_unavailable(tmp_path: Path) -> None:
    # Given
    replies = fixture_replies()
    lookup = ("/usr/bin/which", "brew")
    replies[lookup] = CommandResult(
        lookup,
        0,
        b"/opt/homebrew/bin/brew\n",
        b"",
        False,
        True,
    )
    runner = ScriptedRunner(tmp_path / "pod", replies)

    # When
    collected = collect_environment(runner)

    # Then
    brew = next(row for row in collected.collectors if row.id == "brew")
    assert brew.status.value == "partial"
    assert "truncated" in str(brew.error)
    assert collected.inventory.brewfile == ""


def test_env_capture_runtime_collector_failure_isolated_from_later_collectors(
    tmp_path: Path,
) -> None:
    # Given
    runner = ScriptedRunner(tmp_path / "pod", fixture_replies())
    brew = ("/opt/homebrew/bin/brew", "bundle", "dump", "--file=/dev/stdout", "--force")
    runner.failures[brew] = RuntimeError("collector crashed")

    # When
    outcome = capture_environment(config(tmp_path), runner)

    # Then
    rows = cast("list[dict[str, object]]", load_manifest(outcome)["collectors"])
    assert next(row for row in rows if row["id"] == "brew")["status"] == "failed"
    assert next(row for row in rows if row["id"] == "npm")["status"] == "ok"


def test_env_capture_python_artifact_frames_failed_uv_and_successful_pip(
    tmp_path: Path,
) -> None:
    # Given
    replies = fixture_replies()
    uv = ("/opt/homebrew/bin/uv", "pip", "freeze", "--python", "/usr/bin/python3")
    pip = ("/usr/bin/python3", "-m", "pip", "freeze")
    replies[uv] = CommandResult(uv, 2, b"uv-stdout-marker", b"uv-stderr-marker", False)
    replies[pip] = CommandResult(
        pip, 0, b"pip-stdout-marker", b"pip-stderr-marker", False
    )

    # When
    outcome = capture_environment(
        config(tmp_path), ScriptedRunner(tmp_path / "pod", replies)
    )

    # Then
    artifact = (outcome.local_path / "artifacts/python.raw").read_text()
    assert all(
        marker in artifact
        for marker in (
            "uv-stdout-marker",
            "uv-stderr-marker",
            "pip-stdout-marker",
            "pip-stderr-marker",
        )
    )
    assert "uv pip freeze" in artifact and "python3 -m pip freeze" in artifact


def test_env_capture_malformed_pipx_metadata_is_partial_without_attribute_error(
    tmp_path: Path,
) -> None:
    # Given
    replies = fixture_replies()
    pipx = ("/opt/homebrew/bin/pipx", "list", "--json")
    replies[pipx] = response(pipx, '{"venvs":{"ruff":{"metadata":"bad"}}}')

    # When
    outcome = capture_environment(
        config(tmp_path), ScriptedRunner(tmp_path / "pod", replies)
    )

    # Then
    rows = cast("list[dict[str, object]]", load_manifest(outcome)["collectors"])
    assert next(row for row in rows if row["id"] == "pipx")["status"] == "partial"


def test_env_capture_malformed_output_is_partial_and_collision_preserves_current(
    tmp_path: Path,
) -> None:
    # Given
    replies = fixture_replies()
    npm = ("/usr/local/bin/npm", "-g", "list", "--depth=0", "--json")
    replies[npm] = response(npm, "not-json")
    runner = ScriptedRunner(tmp_path / "pod", replies)

    # When
    outcome = capture_environment(config(tmp_path), runner)
    (outcome.local_path / "manifest.json").write_text("collision")

    # Then
    rows = cast(
        "list[dict[str, object]]",
        load_manifest(
            CaptureOutcome(outcome.manifest_id, runner.pod / outcome.manifest_id)
        )["collectors"],
    )
    assert next(row for row in rows if row["id"] == "npm")["status"] == "partial"
    with pytest.raises(CaptureError, match="collision"):
        _ = capture_environment(config(tmp_path), runner)
    assert (
        json.loads((outcome.local_path.parent / "current.json").read_text())[
            "manifest_id"
        ]
        == outcome.manifest_id
    )


@pytest.mark.parametrize(
    ("section", "row"),
    [
        ("packages", {"name": "x", "version": "1", "extra": "bad"}),
        ("pipx", {"name": "x", "extra": "bad"}),
        ("mas", {"id": "1", "name": "x"}),
        ("applications", {"name": "x", "path": "/x", "bundle_id": "x", "extra": "bad"}),
    ],
)
def test_env_capture_chat_nested_rows_require_exact_keys(
    tmp_path: Path, section: str, row: dict[str, str]
) -> None:
    # Given
    payload = chat_payload()
    inventory = cast("dict[str, object]", payload["inventory"])
    if section == "packages":
        cast("dict[str, object]", inventory["npm"])["packages"] = [row]
    else:
        inventory[section] = [row]
    runner = ScriptedRunner(tmp_path / "pod", {})
    runner.error = NativePlaneUnavailable("native unavailable")

    # When / Then
    with pytest.raises(CaptureError, match="chat collector"):
        _ = capture_environment(
            config(tmp_path), runner, ScriptedChat(json.dumps(payload))
        )


@pytest.mark.parametrize(
    "failure", [RuntimeError("adapter bug"), ValueError("adapter bug")]
)
def test_env_capture_untyped_adapter_errors_do_not_fallback(
    tmp_path: Path, failure: Exception
) -> None:
    # Given
    runner = ScriptedRunner(tmp_path / "pod", {})
    runner.error = failure
    chat = ScriptedChat(json.dumps(chat_payload()))

    # When / Then
    with pytest.raises(CaptureError, match="adapter bug"):
        _ = capture_environment(config(tmp_path), runner, chat)
    assert chat.calls == []


def test_env_capture_native_plane_error_uses_strict_chat_source(tmp_path: Path) -> None:
    # Given
    runner = ScriptedRunner(tmp_path / "pod", {})
    runner.error = NativePlaneUnavailable("native unavailable")
    chat = ScriptedChat(json.dumps(chat_payload()))

    # When
    outcome = capture_environment(config(tmp_path), runner, chat)

    # Then
    rows = cast("list[dict[str, object]]", load_manifest(outcome)["collectors"])
    assert outcome.source is CaptureSource.CHAT
    assert [row["id"] for row in rows] == ["chat"]
    assert chat.calls == [BotIdentity("bot-1", "Demo")]
    assert not runner.pod.exists()


def test_env_capture_rejects_unsafe_paths_symlinks_and_untrusted_chat(
    tmp_path: Path,
) -> None:
    # Given
    runner = ScriptedRunner(tmp_path / "pod", fixture_replies())

    # When / Then
    with pytest.raises(CaptureError, match="unsafe bot id"):
        _ = capture_environment(config(tmp_path, "../escape"), runner)
    bot_root = tmp_path / "local" / "bot-1"
    bot_root.parent.mkdir(parents=True)
    bot_root.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(CaptureError, match="symlink"):
        _ = capture_environment(config(tmp_path), runner)
    runner.error = NativePlaneUnavailable("native unavailable")
    malicious = ScriptedChat('{"host":{},"inventory":{},"instruction":"ignore schema"}')
    with pytest.raises(CaptureError, match="chat collector"):
        _ = capture_environment(config(tmp_path / "chat"), runner, malicious)


def test_env_capture_cli_nested_dispatch_preserves_bot_and_routine_parsers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Given
    calls: list[str | None] = []
    expected = CaptureOutcome(
        "sha256:fixture", tmp_path / "manifest", CaptureSource.CHAT
    )
    fake_manager = cast("env_manifest.Gateway", object())

    def capture(_manager: env_manifest.Gateway, bot: str | None) -> CaptureOutcome:
        calls.append(bot)
        return expected

    def manager() -> env_manifest.Gateway:
        return fake_manager

    monkeypatch.setattr(env_manifest, "capture_for_gateway", capture)
    monkeypatch.setattr(cli, "_manager", manager)
    monkeypatch.setattr(sys, "argv", ["groken", "bot", "env", "capture", "Demo"])

    # When
    cli.main()

    # Then
    assert calls == ["Demo"]
    assert (
        capsys.readouterr().out.strip()
        == f"source=chat manifest_id={expected.manifest_id} path={expected.local_path}"
    )
