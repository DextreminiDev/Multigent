"""Display-name sanitization (ARCHITECTURE.md §7.2) and avatar colors.

Names are rendered into `<message from="NAME">` speaker tags, so the allowed
alphabet excludes everything that could break out of an attribute: letters,
digits, space, `.`, `_`, `-` only; 1-40 chars; no leading/trailing space.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid

MAX_NAME_LEN = 40
_EXTRA = frozenset(" ._-")

# Deterministic per-user avatar colors; readable on dark and light backgrounds.
AVATAR_COLORS = (
    "#e5484d", "#f76b15", "#ffc53d", "#46a758", "#12a594",
    "#0090ff", "#6e56cf", "#d6409f", "#8e8c99", "#ad7f58",
)  # fmt: skip


def _allowed(ch: str) -> bool:
    return ch in _EXTRA or unicodedata.category(ch)[0] in ("L", "N")


def sanitize_display_name(raw: str) -> str:
    """Normalize and validate. Collapses internal whitespace runs to one space and
    drops disallowed characters; raises ValueError if nothing usable remains."""
    name = unicodedata.normalize("NFKC", raw)
    name = " ".join(name.split())  # newlines/tabs -> single spaces, trims ends
    name = "".join(ch for ch in name if _allowed(ch)).strip()
    name = " ".join(name.split())
    if not name:
        raise ValueError("Name must contain letters or digits")
    if len(name) > MAX_NAME_LEN:
        name = name[:MAX_NAME_LEN].rstrip()
    return name


def is_valid_display_name(name: str) -> bool:
    return (
        0 < len(name) <= MAX_NAME_LEN and name == name.strip() and all(_allowed(ch) for ch in name)
    )


def avatar_color(user_id: uuid.UUID) -> str:
    digest = hashlib.sha256(user_id.bytes).digest()
    return AVATAR_COLORS[digest[0] % len(AVATAR_COLORS)]
