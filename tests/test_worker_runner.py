import json
import os
from pathlib import Path

import pytest

from groken.worker_runner import (
    OmoRunner,
    WorkerProtocolError,
    extract_final_text,
    resolve_workspace,
)
from groken.worker_store import SecretStore


def test_extract_final_text_reads_last_assistant_text() -> None:
    event = {
        "type": "agent_end",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "question"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "verified result"},
                ],
            },
        ],
    }

    assert extract_final_text(json.dumps(event)) == "verified result"


def test_extract_final_text_rejects_incomplete_stream() -> None:
    with pytest.raises(WorkerProtocolError, match="agent_end"):
        _ = extract_final_text('{"type":"turn_end"}\n')


def test_resolve_workspace_stays_under_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    assert resolve_workspace(root, "qa/checkout") == root / "qa" / "checkout"


@pytest.mark.parametrize(
    "workspace", ["../escape", "/tmp/escape", "qa/../../escape", ""]
)
def test_resolve_workspace_rejects_escape(tmp_path: Path, workspace: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(ValueError):
        _ = resolve_workspace(root, workspace)


def test_worker_log_uses_atomic_private_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = OmoRunner(
        omo_command="omo",
        state_dir=tmp_path,
        secret_store=SecretStore(tmp_path),
    )
    target = tmp_path / "logs" / "job-1.json"
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []

    def replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync_calls.append)

    runner.write_log("job-1", "stdout", "stderr")

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary.parent == target.parent
    assert destination == target
    assert fsync_calls
    assert (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text()) == {
        "stdout": "stdout",
        "stderr": "stderr",
    }
