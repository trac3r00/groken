from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import groken

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "groken"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PYRIGHT_CONFIG = ROOT / "pyrightconfig.json"
HERO_ASSET = "docs/assets/groken-hero.png"
CURRENT_DOCS = (
    "docs/capabilities-0.30.0.md",
    "docs/direct-worker-runbook.md",
    "docs/native-operation-plane.md",
    "docs/provider-e2e-runbook.md",
)
UNSHIPPED_DOCS = (
    "docs/painpoints-2026-08-19.md",
    "docs/capabilities-0.27.0.md",
)
SHA_PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _project_metadata() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _override_import_sources(source: Path) -> set[str]:
    tree = ast.parse(source.read_text())
    return {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "override" for alias in node.names)
    }


def _workflow_lines() -> list[str]:
    return WORKFLOW.read_text().splitlines()


def test_release_metadata_matches_runtime_when_loaded() -> None:
    # Given
    metadata = _project_metadata()
    project = metadata["project"]
    assert isinstance(project, dict)

    # When
    runtime_version = groken.__version__

    # Then
    assert runtime_version == project["version"] == "0.3.0"


def test_build_backend_and_runtime_compatibility_dependencies_when_loaded() -> None:
    # Given
    metadata = _project_metadata()
    build_system = metadata.get("build-system")
    project = metadata["project"]
    assert isinstance(build_system, dict)
    assert isinstance(project, dict)
    optional = project["optional-dependencies"]
    assert isinstance(optional, dict)

    # When
    requirements = build_system["requires"]
    dependencies = project["dependencies"]
    mcp_dependencies = optional["mcp"]

    # Then
    assert requirements == ["setuptools==84.0.0"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    dependency_groups = metadata["dependency-groups"]
    assert isinstance(dependency_groups, dict)
    dev_dependencies = dependency_groups["dev"]

    assert "typing_extensions>=4.12" in dependencies
    assert "pydantic>=2.11" in dependencies
    assert mcp_dependencies == ["mcp>=2.0,<2.1"]
    assert "ruff==0.16.2" in dev_dependencies
    assert "basedpyright==1.39.8" in dev_dependencies


def test_python311_compatibility_imports_override_from_backport_when_loaded() -> None:
    # Given
    modules = sorted(PACKAGE.glob("*.py"))
    assert modules

    # When
    sources_by_module = {
        module.name: _override_import_sources(module) for module in modules
    }

    # Then
    importers = {name: src for name, src in sources_by_module.items() if src}
    assert importers
    assert all(sources == {"typing_extensions"} for sources in importers.values())


def test_hero_asset_ships_in_distributions_when_packaged() -> None:
    # Given
    metadata = _project_metadata()
    tool = metadata["tool"]
    assert isinstance(tool, dict)
    setuptools_config = tool["setuptools"]
    assert isinstance(setuptools_config, dict)

    # When
    data_files = setuptools_config["data-files"]

    # Then
    assert isinstance(data_files, dict)
    assert HERO_ASSET in data_files["docs/assets"]
    assert (ROOT / HERO_ASSET).is_file()
    assert data_files["docs"] == list(CURRENT_DOCS)
    shipped = set(data_files["docs"])
    assert shipped.isdisjoint(UNSHIPPED_DOCS)
    assert "docs/*.md" not in shipped


def test_ci_workflow_restricts_token_and_pins_actions_when_configured() -> None:
    # Given
    lines = _workflow_lines()

    # When
    permissions_at = lines.index("permissions:")
    action_refs = [
        line.split("uses:", 1)[1].split("#", 1)[0].strip()
        for line in lines
        if "uses:" in line
    ]

    # Then
    assert lines[permissions_at + 1] == "  contents: read"
    assert action_refs
    assert all(SHA_PINNED_ACTION.match(ref) for ref in action_refs)


def test_ci_workflow_tests_supported_pythons_deterministically_when_configured() -> None:
    # Given
    text = WORKFLOW.read_text()

    # When
    matrix_line = next(
        line for line in text.splitlines() if line.strip().startswith("python-version:")
    )
    matrix_versions = set(re.findall(r'"(\d+\.\d+)"', matrix_line))

    # Then
    assert matrix_versions == {"3.11", "3.12", "3.13"}
    assert "uv sync --locked --all-extras" in text
    assert "uv run --no-sync ruff check groken tests" in text
    assert "uvx ruff" not in text
    assert "uv pip install --python" in text
    assert "install codex-skills" in text


def test_ci_pins_runner_uv_and_runs_an_error_only_type_gate_when_configured() -> None:
    # Given
    text = WORKFLOW.read_text()
    config: dict[str, str] = json.loads(PYRIGHT_CONFIG.read_text())

    # When
    runners = [
        line.strip().removeprefix("runs-on: ")
        for line in text.splitlines()
        if line.strip().startswith("runs-on:")
    ]

    # Then
    assert runners == ["macos-15", "macos-15", "macos-15"]
    assert text.count('version: "0.11.30"') == 3
    assert "uv run --no-sync basedpyright --level error" in text
    assert "uvx --from basedpyright" not in text
    assert config["typeCheckingMode"] == "standard"


def test_process_named_task_suites_are_gone_when_loaded() -> None:
    # Given
    tests = ROOT / "tests"

    # When
    leftover = sorted(path.name for path in tests.glob("test_task*.py"))

    # Then
    assert leftover == []
