import ast
from pathlib import Path

from groken.capabilities import CURRENT_030_COMMAND_NAMES


def production_command_literals() -> dict[str, list[str]]:
    commands: dict[str, list[str]] = {}
    for path in sorted(Path("groken").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in {"command", "command_once"} or not node.args:
                continue
            method = node.args[0]
            if isinstance(method, ast.Constant) and isinstance(method.value, str):
                commands.setdefault(method.value, []).append(f"{path}:{node.lineno}")
    return commands


def test_every_production_command_literal_exists_in_030_inventory() -> None:
    # Given
    commands = production_command_literals()

    # When
    unsupported = sorted(set(commands) - set(CURRENT_030_COMMAND_NAMES))

    # Then
    assert unsupported == [], {name: commands[name] for name in unsupported}
