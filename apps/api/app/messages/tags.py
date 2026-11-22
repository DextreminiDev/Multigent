"""Message tags and chat reply modes.

| Tag   | Calls the AI | AI sees it as context |
|-------|--------------|-----------------------|
| @ai   | yes          | yes                   |
| @skip | no           | yes                   |
| @off  | no           | no (humans only)      |
| none  | "always" mode: yes; "tags" mode: no | yes |

Tags are whole words anywhere in the message, case-insensitive. If several appear,
the most restrictive wins (@off > @skip > @ai), so a message can never spend the
owner's tokens or reach the model by accident. The server is the source of truth;
the web client mirrors this for the composer preview.
"""

from __future__ import annotations

import re
from typing import Literal

Intent = Literal["reply", "context", "off"]
ReplyMode = Literal["always", "tags"]

# "@ai" not preceded by a word char or "@" (so emails like bob@ai.com don't count)
TAG_RE = re.compile(r"(?<![\w@.])@(ai|skip|off)\b", re.IGNORECASE)


def tags_in(content: str) -> set[str]:
    return {m.group(1).lower() for m in TAG_RE.finditer(content)}


def parse_intent(content: str, mode: str) -> Intent:
    tags = tags_in(content)
    if "off" in tags:
        return "off"
    if "skip" in tags:
        return "context"
    if "ai" in tags:
        return "reply"
    return "reply" if mode == "always" else "context"
