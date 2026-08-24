"""Fail-closed construction of the ExecService shell parsing result."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_PLACEHOLDER_PREFIX = "\x07GROKEN_SUB_"


class _ParseError(ValueError):
    pass


@dataclass(frozen=True)
class _Substitution:
    start: int
    end: int
    content: str


def _failed_result() -> dict:
    return {
        "parsing_failed": True,
        "executable_commands": [],
        "has_redirects": False,
        "has_command_substitution": False,
    }


def _valid_result(commands: list[dict], redirects: bool, substitutions: bool) -> dict:
    return {
        "parsing_failed": False,
        "executable_commands": commands,
        "has_redirects": redirects,
        "has_command_substitution": substitutions,
    }


def _consume_single_quote(text: str, start: int) -> int:
    end = text.find("'", start + 1)
    if end < 0:
        raise _ParseError("unterminated single quote")
    return end + 1


def _consume_double_quote(text: str, start: int) -> tuple[int, list[_Substitution]]:
    substitutions: list[_Substitution] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text):
                raise _ParseError("dangling escape")
            index += 2
        elif char == '"':
            return index + 1, substitutions
        elif text.startswith("$(", index):
            substitution = _consume_parenthesized_substitution(text, index)
            substitutions.append(substitution)
            index = substitution.end
        elif char == "`":
            substitution = _consume_backtick_substitution(text, index)
            substitutions.append(substitution)
            index = substitution.end
        else:
            index += 1
    raise _ParseError("unterminated double quote")


def _consume_parenthesized_substitution(text: str, start: int) -> _Substitution:
    content_start = start + 2
    end, _ = _scan_level(text, content_start, closing=")")
    content = text[content_start : end - 1]
    if not content.strip():
        raise _ParseError("empty command substitution")
    return _Substitution(start, end, content)


def _consume_backtick_substitution(text: str, start: int) -> _Substitution:
    content_start = start + 1
    end, _ = _scan_level(text, content_start, closing="`")
    content = text[content_start : end - 1]
    if not content.strip():
        raise _ParseError("empty command substitution")
    return _Substitution(start, end, content)


def _scan_level(
    text: str, start: int = 0, *, closing: str | None = None
) -> tuple[int, list[_Substitution]]:
    """Validate quotes/substitutions and return substitutions at this level."""
    substitutions: list[_Substitution] = []
    index = start
    while index < len(text):
        char = text[index]
        if closing is not None and char == closing:
            return index + 1, substitutions
        if char == "\\":
            if index + 1 >= len(text):
                raise _ParseError("dangling escape")
            index += 2
        elif char == "'":
            index = _consume_single_quote(text, index)
        elif char == '"':
            index, quoted_substitutions = _consume_double_quote(text, index)
            substitutions.extend(quoted_substitutions)
        elif text.startswith("$(", index) or text.startswith("<(", index):
            substitution = _consume_parenthesized_substitution(text, index)
            substitutions.append(substitution)
            index = substitution.end
        elif char == "`":
            substitution = _consume_backtick_substitution(text, index)
            substitutions.append(substitution)
            index = substitution.end
        elif char in "()":
            # Grouping, subshells, and arithmetic syntax are outside this small
            # parser's representable grammar, so accepting them would be unsafe.
            raise _ParseError("unsupported parenthesis")
        else:
            index += 1
    if closing is not None:
        raise _ParseError("unterminated command substitution")
    return index, substitutions


def _substitution_map(substitutions: list[_Substitution]) -> dict[int, _Substitution]:
    return {substitution.start: substitution for substitution in substitutions}


def _skip_double_quote(text: str, start: int) -> int:
    end, _ = _consume_double_quote(text, start)
    return end


def _split_commands(text: str, substitutions: list[_Substitution]) -> list[str]:
    by_start = _substitution_map(substitutions)
    segments: list[str] = []
    segment_start = 0
    index = 0
    while index < len(text):
        substitution = by_start.get(index)
        if substitution is not None:
            index = substitution.end
            continue
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "'":
            index = _consume_single_quote(text, index)
            continue
        if char == '"':
            index = _skip_double_quote(text, index)
            continue

        if char in "<>":
            operator = text[index : index + 2]
            index += 2 if operator in {">>", "<<", "<>", ">|"} else 1
            index = _consume_redirect_target(text, index, by_start)
            continue

        operator_length = 0
        if text.startswith("&&", index) or text.startswith("||", index):
            operator_length = 2
        elif char in "|;":
            operator_length = 1
        elif char == "&":
            raise _ParseError("unsupported bare ampersand")
        elif char == "\n":
            operator_length = 1

        if operator_length:
            segment = text[segment_start:index].strip()
            if not segment:
                raise _ParseError("empty command segment")
            segments.append(segment)
            index += operator_length
            segment_start = index
        else:
            index += 1

    final_segment = text[segment_start:].strip()
    if final_segment:
        segments.append(final_segment)
    elif segments:
        raise _ParseError("dangling command operator")
    return segments


def _consume_redirect_target(
    text: str, start: int, substitutions: dict[int, _Substitution]
) -> int:
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        raise _ParseError("redirect without target")

    if text[index] == "&":
        match = re.match(r"&(?:[0-9]+|-)(?=$|\s)", text[index:])
        if match is None:
            raise _ParseError("invalid descriptor redirect")
        return index + match.end()

    target_start = index
    while index < len(text):
        substitution = substitutions.get(index)
        if substitution is not None:
            index = substitution.end
            continue
        char = text[index]
        if char.isspace() or char in "|;&><":
            break
        if char == "\\":
            if index + 1 >= len(text):
                raise _ParseError("dangling escape")
            index += 2
        elif char == "'":
            index = _consume_single_quote(text, index)
        elif char == '"':
            index = _skip_double_quote(text, index)
        else:
            index += 1
    if index == target_start:
        raise _ParseError("redirect without target")
    return index


def _strip_redirects(
    text: str, substitutions: list[_Substitution]
) -> tuple[str, bool]:
    by_start = _substitution_map(substitutions)
    output: list[str] = []
    found_redirect = False
    index = 0
    while index < len(text):
        substitution = by_start.get(index)
        if substitution is not None:
            output.append(text[index : substitution.end])
            index = substitution.end
            continue
        char = text[index]
        if char == "\\":
            output.append(text[index : index + 2])
            index += 2
            continue
        if char == "'":
            end = _consume_single_quote(text, index)
            output.append(text[index:end])
            index = end
            continue
        if char == '"':
            end = _skip_double_quote(text, index)
            output.append(text[index:end])
            index = end
            continue

        redirect_start = index
        if char.isdigit() and (index == 0 or text[index - 1].isspace()):
            while index < len(text) and text[index].isdigit():
                index += 1
            if index >= len(text) or text[index] not in "<>":
                output.append(text[redirect_start:index])
                continue
        elif char not in "<>":
            output.append(char)
            index += 1
            continue

        operator = text[index : index + 2]
        if operator.startswith("<<") or operator in {"<>", ">|"}:
            raise _ParseError("unsupported redirect operator")
        index += 2 if operator == ">>" else 1
        index = _consume_redirect_target(text, index, by_start)
        found_redirect = True
        if output and not output[-1].endswith(tuple(" \t\n")):
            output.append(" ")

    return "".join(output).strip(), found_redirect


def _word_end(text: str, start: int, substitutions: dict[int, _Substitution]) -> int:
    index = start
    while index < len(text) and not text[index].isspace():
        substitution = substitutions.get(index)
        if substitution is not None:
            index = substitution.end
        elif text[index] == "\\":
            if index + 1 >= len(text):
                raise _ParseError("dangling escape")
            index += 2
        elif text[index] == "'":
            index = _consume_single_quote(text, index)
        elif text[index] == '"':
            index = _skip_double_quote(text, index)
        else:
            index += 1
    return index


def _shell_words(text: str) -> list[str]:
    _, substitutions = _scan_level(text)
    if _PLACEHOLDER_PREFIX in text:
        raise _ParseError("reserved control sequence")

    replacements: dict[str, str] = {}
    protected = text
    for number, substitution in reversed(list(enumerate(substitutions))):
        placeholder = f"{_PLACEHOLDER_PREFIX}{number}\x07"
        replacements[placeholder] = protected[substitution.start : substitution.end]
        protected = (
            protected[: substitution.start]
            + placeholder
            + protected[substitution.end :]
        )
    try:
        words = shlex.split(protected, comments=False, posix=True)
    except ValueError as error:
        raise _ParseError(str(error)) from error
    return [_restore_substitutions(word, replacements) for word in words]


def _restore_substitutions(word: str, replacements: dict[str, str]) -> str:
    for placeholder, source in replacements.items():
        word = word.replace(placeholder, source)
    return word


def _without_assignment_prefix(text: str) -> str:
    _, substitutions = _scan_level(text)
    by_start = _substitution_map(substitutions)
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        end = _word_end(text, index, by_start)
        raw_word = text[index:end]
        words = _shell_words(raw_word)
        if len(words) != 1 or _ASSIGNMENT.fullmatch(words[0]) is None:
            return text[index:].strip()
        index = end
    return ""


def _parse_text(text: str) -> tuple[list[dict], bool, bool]:
    _, substitutions = _scan_level(text)
    segments = _split_commands(text, substitutions)
    commands: list[dict] = []
    has_redirects = False
    has_substitutions = bool(substitutions)

    for segment in segments:
        _, segment_substitutions = _scan_level(segment)
        command_text, redirects = _strip_redirects(segment, segment_substitutions)
        has_redirects = has_redirects or redirects
        executable_text = _without_assignment_prefix(command_text)
        if executable_text:
            words = _shell_words(executable_text)
            if not words:
                raise _ParseError("missing executable")
            commands.append(
                {
                    "name": words[0],
                    "args": [
                        {"type": "ARG", "value": argument}
                        for argument in words[1:]
                    ],
                    "full_text": executable_text,
                }
            )

        for substitution in segment_substitutions:
            nested_commands, nested_redirects, _ = _parse_text(substitution.content)
            commands.extend(nested_commands)
            has_redirects = has_redirects or nested_redirects
            has_substitutions = True

    return commands, has_redirects, has_substitutions


def build_parsing_result(shell_text: str) -> dict:
    """Build the daemon's parsing_result object, failing closed on uncertainty."""
    if not isinstance(shell_text, str):
        return _failed_result()
    if not shell_text.strip():
        return _valid_result([], False, False)
    try:
        commands, redirects, substitutions = _parse_text(shell_text)
    except (_ParseError, RecursionError):
        return _failed_result()
    return _valid_result(commands, redirects, substitutions)
