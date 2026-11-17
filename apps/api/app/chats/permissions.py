"""The permissions matrix (prompt §6). This is the ONLY place roles are
interpreted (ARCHITECTURE.md §11.2); REST and WS handlers both call `authorize`."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

Role = Literal["owner", "editor", "viewer"]


class Action(StrEnum):
    READ = "read"  # read messages / watch live
    SEND = "send"  # send prompts
    DELETE_OWN_PENDING = "delete_own_pending"
    CANCEL_TURN = "cancel_turn"  # editor: only if their message is in the batch
    RETRY_TURN = "retry_turn"  # same rule as cancel (re-run a failed/stopped batch)
    REGENERATE = "regenerate"
    MANAGE_CHAT = "manage_chat"  # title / instructions / model / key / caps
    MANAGE_MEMBERS = "manage_members"  # invites, roles, removal
    LEAVE = "leave"
    DELETE_CHAT = "delete_chat"
    VIEW_PRIVATE_SETTINGS = "view_private_settings"  # instructions, caps, usage, key hint


_ALL = frozenset(Action)
_EDITOR = frozenset(
    {
        Action.READ,
        Action.SEND,
        Action.DELETE_OWN_PENDING,
        Action.CANCEL_TURN,
        Action.RETRY_TURN,
        Action.LEAVE,
    }
)
_VIEWER = frozenset({Action.READ, Action.LEAVE})

MATRIX: dict[Role, frozenset[Action]] = {
    "owner": _ALL - {Action.LEAVE},  # owner must delete or transfer instead
    "editor": _EDITOR,
    "viewer": _VIEWER,
}


def authorize(role: Role | None, action: Action, *, in_batch: bool = False) -> bool:
    """`role` None means not a (current) member: nothing is allowed.
    `in_batch`: for CANCEL_TURN, whether the user authored a message in the batch."""
    if role is None or action not in MATRIX[role]:
        return False
    if action in (Action.CANCEL_TURN, Action.RETRY_TURN) and role == "editor":
        return in_batch
    return True
