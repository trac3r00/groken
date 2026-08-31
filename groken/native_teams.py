from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias, cast

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
MAX_TEAM_MEMBERS = 6
MIN_TEAM_MEMBERS = 2
MAX_TEAM_LABEL_BYTES = 128


class NativeTeamError(Exception):
    pass


class NativeTeamGateway(Protocol):
    def command(
        self, method: str, args: dict[str, JsonValue] | None = None
    ) -> JsonValue: ...

    def command_once(self, method: str, args: dict[str, JsonValue]) -> JsonValue: ...

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str: ...


@dataclass(frozen=True, slots=True)
class NativeTeamMember:
    agent_id: str
    name: str


@dataclass(frozen=True, slots=True)
class NativeTeam:
    team_id: str
    name: str
    description: str
    members: tuple[NativeTeamMember, ...]


def _object(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise NativeTeamError(f"invalid {label}")
    return cast("dict[str, JsonValue]", value)


def _roster(manager: NativeTeamGateway) -> tuple[dict[str, JsonValue], ...]:
    raw = manager.command("listAgents")
    if not isinstance(raw, list):
        raise NativeTeamError("invalid Bot roster")
    return tuple(_object(item, "Bot roster row") for item in raw)


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeTeamError(f"invalid {label}")
    return value.strip()


def _label(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise NativeTeamError(f"{label} must not be empty")
    if len(normalized.encode()) > MAX_TEAM_LABEL_BYTES:
        raise NativeTeamError(f"{label} exceeds {MAX_TEAM_LABEL_BYTES} bytes")
    return normalized


def _is_native_team(row: dict[str, JsonValue]) -> bool:
    return row.get("isGroup") is True and row.get("isSharedRoom") is not True


def _resolve(
    roster: tuple[dict[str, JsonValue], ...], selector: str, *, team: bool
) -> dict[str, JsonValue]:
    normalized = _label(selector, "team" if team else "Bot selector")
    candidates = [
        row
        for row in roster
        if _is_native_team(row) is team
        and (row.get("id") == normalized or row.get("name") == normalized)
    ]
    if not candidates:
        if not team and any(
            _is_native_team(row)
            and (row.get("id") == normalized or row.get("name") == normalized)
            for row in roster
        ):
            raise NativeTeamError(f"team cannot contain a team: {normalized}")
        kind = "team" if team else "Bot"
        raise NativeTeamError(f"unknown {kind}: {normalized}")
    if len(candidates) > 1:
        kind = "team" if team else "Bot"
        raise NativeTeamError(f"ambiguous {kind}: {normalized}")
    return candidates[0]


def _member(
    roster: tuple[dict[str, JsonValue], ...], agent_id: str
) -> NativeTeamMember:
    rows = [row for row in roster if row.get("id") == agent_id]
    if len(rows) != 1 or _is_native_team(rows[0]):
        raise NativeTeamError(f"team member missing from roster: {agent_id}")
    return NativeTeamMember(agent_id, _text(rows[0].get("name"), "Bot name"))


def _team_from_row(
    roster: tuple[dict[str, JsonValue], ...], row: dict[str, JsonValue]
) -> NativeTeam:
    raw_members = row.get("memberIds")
    if not isinstance(raw_members, list) or not all(
        isinstance(member_id, str) and member_id for member_id in raw_members
    ):
        raise NativeTeamError("invalid native team memberIds")
    member_ids = cast("list[str]", raw_members)
    description = row.get("description")
    return NativeTeam(
        _text(row.get("id"), "team id"),
        _text(row.get("name"), "team name"),
        description if isinstance(description, str) else "",
        tuple(_member(roster, member_id) for member_id in member_ids),
    )


def create_native_team(
    manager: NativeTeamGateway,
    name: str,
    members: Sequence[str],
    description: str = "",
) -> NativeTeam:
    normalized_name = _label(name, "team name")
    selectors = tuple(_label(member, "Bot selector") for member in members)
    if len(selectors) < MIN_TEAM_MEMBERS:
        raise NativeTeamError(f"a native team needs at least {MIN_TEAM_MEMBERS} Bots")
    if len(selectors) > MAX_TEAM_MEMBERS:
        raise NativeTeamError(f"a native team supports at most {MAX_TEAM_MEMBERS} Bots")
    if len(set(selectors)) != len(selectors):
        raise NativeTeamError("duplicate Bot selector")
    roster = _roster(manager)
    if any(
        _is_native_team(row) and row.get("name") == normalized_name for row in roster
    ):
        raise NativeTeamError(f"team already exists: {normalized_name}")
    member_rows = tuple(
        _resolve(roster, selector, team=False) for selector in selectors
    )
    member_ids = tuple(_text(row.get("id"), "Bot id") for row in member_rows)
    if len(set(member_ids)) != len(member_ids):
        raise NativeTeamError("duplicate Bot selection")
    raw = manager.command_once(
        "createGroup",
        {
            "name": normalized_name,
            "description": description.strip(),
            "memberAgentIds": list(member_ids),
        },
    )
    created = _object(_object(raw, "createGroup response").get("agent"), "team agent")
    team_id = _text(created.get("id"), "team id")
    created_name = created.get("name")
    return NativeTeam(
        team_id,
        created_name.strip()
        if isinstance(created_name, str) and created_name.strip()
        else normalized_name,
        description.strip(),
        tuple(_member(roster, member_id) for member_id in member_ids),
    )


def get_native_team(manager: NativeTeamGateway, selector: str) -> NativeTeam:
    roster = _roster(manager)
    return _team_from_row(roster, _resolve(roster, selector, team=True))


def ask_native_team(
    manager: NativeTeamGateway,
    selector: str,
    text: str,
    timeout_s: float = 600,
) -> str:
    if not text.strip():
        raise NativeTeamError("task text must not be empty")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise NativeTeamError("timeout must be a positive finite number")
    team = get_native_team(manager, selector)
    return manager.ask(team.team_id, text, timeout_s)
