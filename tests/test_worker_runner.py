import json
from pathlib import Path

import pytest

from groken.worker_runner import (
    WorkerProtocolError,
    extract_final_text,
    resolve_workspace,
)


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


@pytest.mark.parametrize("workspace", ["../escape", "/tmp/escape", "qa/../../escape", ""])
def test_resolve_workspace_rejects_escape(tmp_path: Path, workspace: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(ValueError):
        _ = resolve_workspace(root, workspace)
