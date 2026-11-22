"""Mentions and off the record (ARCHITECTURE.md §6.2b).

The AI replies only to messages that mention it. Off-the-record messages are never
shown to the AI. Mentions are entities: ranges over the plain message text, picked
from the composer's @ menu, like Telegram's MessageEntity. Typing "@ai" by hand is
plain text. Offsets/lengths are UTF-16 code units, which is what browsers count, so
they're converted here before slicing Python strings.

| Message                     | AI replies | AI sees it | to=              |
|-----------------------------|------------|------------|------------------|
| no mentions / @everyone     | no         | yes        | (everyone)       |
| @Bob                        | no         | yes        | Bob              |
| @ai (+ anyone)              | yes        | yes        | (assistant), ... |
| off the record              | no         | no         | (not in prompt)  |
| off the record + @ai        | rejected: 422 off_with_ai                     |
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.errors import ApiError

Intent = Literal["reply", "context", "off"]
MentionType = Literal["ai", "everyone", "user"]
MAX_MENTIONS = 50

# Prompt labels for the special recipients. Display names can't contain parentheses
# (app/users/names.py), so these can never collide with a person's label.
AI_LABEL = "(assistant)"
EVERYONE_LABEL = "(everyone)"


class MentionIn(BaseModel):
    type: MentionType
    offset: int = Field(ge=0)
    length: int = Field(ge=1, le=200)
    user_id: uuid.UUID | None = None


def _u16_index(text: str) -> dict[int, int]:
    """UTF-16 offset -> Python index, for every character boundary."""
    out: dict[int, int] = {}
    u = 0
    for i, ch in enumerate(text):
        out[u] = i
        u += 2 if ord(ch) > 0xFFFF else 1
    out[u] = len(text)
    return out


@dataclass(frozen=True)
class Span:
    start: int  # Python indexes into the text
    end: int
    mention: dict[str, Any]


def spans(text: str, mentions: Sequence[Mapping[str, Any]]) -> list[Span]:
    """Valid mention spans in text order (bad ranges are skipped, never raised)."""
    idx = _u16_index(text)
    out: list[Span] = []
    for m in mentions:
        start = idx.get(int(m.get("offset", -1)))
        end = idx.get(int(m.get("offset", -1)) + int(m.get("length", 0)))
        if start is None or end is None or end <= start or not text[start:end].startswith("@"):
            continue
        out.append(Span(start, end, dict(m)))
    out.sort(key=lambda s: s.start)
    kept: list[Span] = []
    for s in out:
        if kept and s.start < kept[-1].end:
            continue  # overlapping: keep the first
        kept.append(s)
    return kept


def normalize(
    text: str,
    mentions: Sequence[MentionIn],
    *,
    sender_id: uuid.UUID,
    members: Mapping[uuid.UUID, str],
) -> list[dict[str, Any]]:
    """Keep the mentions that make sense; drop the rest (their text stays as plain
    text). Dropping covers honest races, e.g. someone removed between being picked and
    the message being sent. User mentions get the member's current name as a snapshot."""
    raw = [m.model_dump(mode="json", exclude_none=True) for m in mentions[:MAX_MENTIONS]]
    out: list[dict[str, Any]] = []
    for s in spans(text, raw):
        m = s.mention
        if m["type"] == "user":
            uid = uuid.UUID(m["user_id"]) if m.get("user_id") else None
            if uid is None or uid == sender_id or uid not in members:
                continue
            m["name"] = members[uid]
        else:
            m.pop("user_id", None)
        out.append(m)
    return out


def intent_for(mentions: Sequence[Mapping[str, Any]], *, off: bool) -> Intent:
    mentions_ai = any(m["type"] == "ai" for m in mentions)
    if off:
        if mentions_ai:
            raise ApiError(
                422, "off_with_ai", "The AI can't be mentioned in an off-the-record message."
            )
        return "off"
    return "reply" if mentions_ai else "context"


def render_for_prompt(
    text: str,
    mentions: Sequence[Mapping[str, Any]],
    labels: Mapping[uuid.UUID, str],
    *,
    intent: str | None,
) -> tuple[list[tuple[str, bool]], list[str]]:
    """Split text into (segment, is_mention) parts with mentions replaced by prompt
    labels, and compute the `to` recipients. Escaping is left to the caller.

    Messages from before mentions existed have none: their stored intent says whether
    they asked the AI."""
    parts: list[tuple[str, bool]] = []
    to: list[str] = []
    pos = 0
    for s in spans(text, mentions):
        m = s.mention
        if m["type"] == "ai":
            label = AI_LABEL
            shown = "@assistant"
        elif m["type"] == "everyone":
            label = EVERYONE_LABEL
            shown = "@everyone"
        else:
            uid = uuid.UUID(str(m.get("user_id")))
            label = labels.get(uid) or str(m.get("name") or "former member")
            shown = f"@{label}"
        if s.start > pos:
            parts.append((text[pos : s.start], False))
        parts.append((shown, True))
        pos = s.end
        if label not in to:
            to.append(label)
    if pos < len(text):
        parts.append((text[pos:], False))
    if not mentions and intent == "reply":
        to = [AI_LABEL]  # legacy: sent when untagged messages always asked the AI
    if not to:
        to = [EVERYONE_LABEL]
    return parts, to
