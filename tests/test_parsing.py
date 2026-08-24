import pytest

from groken.parsing import build_parsing_result


ARG = "ARG"


def command(name, *args, full_text=None):
    return {
        "name": name,
        "args": [{"type": ARG, "value": value} for value in args],
        "full_text": full_text if full_text is not None else " ".join((name, *args)),
    }


def result(commands=(), *, redirects=False, substitution=False):
    return {
        "parsing_failed": False,
        "executable_commands": list(commands),
        "has_redirects": redirects,
        "has_command_substitution": substitution,
    }


# Appendix C parsingResult contract table.
CONTRACT_CASES = [
    ("", result()),
    ("   \t\n", result()),
    (
        'echo "a|b" | grep b',
        result(
            [
                command("echo", "a|b", full_text='echo "a|b"'),
                command("grep", "b"),
            ]
        ),
    ),
    (
        "a && b || c; d",
        result([command("a"), command("b"), command("c"), command("d")]),
    ),
    ("FOO=1 cmd x", result([command("cmd", "x")])),
    ("FOO=1", result()),
    (
        "sudo -u bob cmd x",
        result([command("sudo", "-u", "bob", "cmd", "x")]),
    ),
    (
        "cmd >out 2>&1",
        result([command("cmd")], redirects=True),
    ),
    (
        "echo '$(date)'",
        result([command("echo", "$(date)", full_text="echo '$(date)'")]),
    ),
    (
        'echo "$(date)"',
        result(
            [
                command("echo", "$(date)", full_text='echo "$(date)"'),
                command("date"),
            ],
            substitution=True,
        ),
    ),
    (
        "cat <(gen)",
        result(
            [command("cat", "<(gen)"), command("gen")],
            substitution=True,
        ),
    ),
    (
        'echo ">"',
        result([command("echo", ">", full_text='echo ">"')]),
    ),
]


@pytest.mark.parametrize(("shell_text", "expected"), CONTRACT_CASES)
def test_appendix_c_contract(shell_text, expected):
    assert build_parsing_result(shell_text) == expected


@pytest.mark.parametrize(
    "shell_text",
    [
        'echo "unterminated',
        "echo $(date",
        "echo `date",
        'a && echo "unterminated',
        "a & b",
        "()",
        "$( )",
    ],
)
def test_malformed_or_ambiguous_input_fails_closed(shell_text):
    assert build_parsing_result(shell_text) == {
        "parsing_failed": True,
        "executable_commands": [],
        "has_redirects": False,
        "has_command_substitution": False,
    }


def test_backtick_substitution_is_reported_and_parsed():
    assert build_parsing_result("echo `date`") == result(
        [command("echo", "`date`", full_text="echo `date`"), command("date")],
        substitution=True,
    )


def test_result_uses_only_the_wire_contract_keys():
    parsed = build_parsing_result("printf %s ok")

    assert set(parsed) == {
        "parsing_failed",
        "executable_commands",
        "has_redirects",
        "has_command_substitution",
    }
    assert set(parsed["executable_commands"][0]) == {"name", "args", "full_text"}
    assert set(parsed["executable_commands"][0]["args"][0]) == {"type", "value"}
