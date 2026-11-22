"""reply mode and message intent

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chats", sa.Column("reply_mode", sa.Text(), server_default="always", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_chats_reply_mode"), "chats", "reply_mode IN ('always','tags')"
    )
    op.add_column("messages", sa.Column("intent", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_messages_intent"),
        "messages",
        "intent IS NULL OR intent IN ('reply','context','off')",
    )
    # every human message sent before modes existed asked the AI
    op.execute("UPDATE messages SET intent = 'reply' WHERE kind = 'human'")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_messages_intent"), "messages", type_="check")
    op.drop_column("messages", "intent")
    op.drop_constraint(op.f("ck_chats_reply_mode"), "chats", type_="check")
    op.drop_column("chats", "reply_mode")
