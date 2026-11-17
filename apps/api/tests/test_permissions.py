"""Table-driven check of every (role x action) cell of the permissions matrix
(prompt §6, ARCHITECTURE.md §11.2)."""

from __future__ import annotations

import pytest

from app.chats.permissions import Action, Role, authorize

A = Action
# Expected outcome per role: (owner, editor, viewer). "batch" = only if in_batch.
EXPECTED: dict[Action, tuple[bool, bool | str, bool]] = {
    A.READ: (True, True, True),
    A.SEND: (True, True, False),
    A.DELETE_OWN_PENDING: (True, True, False),
    A.CANCEL_TURN: (True, "batch", False),
    A.RETRY_TURN: (True, "batch", False),
    A.REGENERATE: (True, False, False),
    A.MANAGE_CHAT: (True, False, False),
    A.MANAGE_MEMBERS: (True, False, False),
    A.LEAVE: (False, True, True),
    A.DELETE_CHAT: (True, False, False),
    A.VIEW_PRIVATE_SETTINGS: (True, False, False),
}
ROLES: tuple[Role, ...] = ("owner", "editor", "viewer")


def test_every_action_is_covered() -> None:
    assert set(EXPECTED) == set(Action)


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("role_idx", range(3))
def test_matrix(action: Action, role_idx: int) -> None:
    role = ROLES[role_idx]
    expected = EXPECTED[action][role_idx]
    if expected == "batch":
        assert authorize(role, action, in_batch=True) is True
        assert authorize(role, action, in_batch=False) is False
    else:
        assert authorize(role, action) is expected
        assert authorize(role, action, in_batch=True) is expected


@pytest.mark.parametrize("action", list(Action))
def test_non_member_can_do_nothing(action: Action) -> None:
    assert authorize(None, action) is False
    assert authorize(None, action, in_batch=True) is False
