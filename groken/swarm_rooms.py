"""Strict parsing and rendering for read-only sharing rooms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

Payload_co = TypeVar("Payload_co", covariant=True)


class RoomManager(Protocol[Payload_co]):
    def command(self, method: str) -> Payload_co: ...


class SwarmError(Exception):
    """Typed swarm boundary error with a stable user-facing reason."""

    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Room:
    room_id: str
    name: str | None
    members: tuple[str, ...]


def read_rooms(manager: RoomManager[Payload_co]) -> tuple[Room, ...]:
    """Read a strict sharing-state room list without issuing mutations."""
    state = manager.command("getSharingState")
    if not isinstance(state, dict) or "rooms" not in state:
        raise SwarmError("malformed sharing response: rooms list is missing")
    raw_rooms = state["rooms"]
    if not isinstance(raw_rooms, list):
        raise SwarmError("malformed sharing response: rooms must be a list")
    rooms: list[Room] = []
    for raw_room in raw_rooms:
        if not isinstance(raw_room, dict):
            raise SwarmError("malformed sharing response: room must be an object")
        room_id = raw_room.get("roomId")
        if not isinstance(room_id, str) or not room_id:
            raise SwarmError("malformed sharing response: roomId must be non-empty")
        raw_name = raw_room.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else None
        members: list[str] = []
        raw_members = raw_room.get("members")
        if isinstance(raw_members, list):
            for raw_member in raw_members:
                if not isinstance(raw_member, dict):
                    continue
                label = raw_member.get("name") or raw_member.get("id")
                if isinstance(label, str) and label:
                    members.append(label)
        rooms.append(Room(room_id, name, tuple(members)))
    return tuple(rooms)


def render_rooms(rooms: tuple[Room, ...]) -> str:
    """Render a declaration and summary of the read-only room state."""
    notice = "Read-only: groken never creates, joins, or leaves shared rooms."
    if not rooms:
        return f"{notice}\nNo shared rooms."
    lines = [notice]
    for room in rooms:
        title = f" ({room.name})" if room.name is not None else ""
        members = f"; members: {', '.join(room.members)}" if room.members else ""
        lines.append(f"- {room.room_id}{title}{members}")
    return "\n".join(lines)
