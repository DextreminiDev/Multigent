"""mentions replace reply modes

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "mentions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    # the AI now replies only when mentioned; existing rows keep their intent
    op.drop_constraint(op.f("ck_chats_reply_mode"), "chats", type_="check")
    op.drop_column("chats", "reply_mode")


def downgrade() -> None:
    op.add_column(
        "chats", sa.Column("reply_mode", sa.Text(), server_default="always", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_chats_reply_mode"), "chats", "reply_mode IN ('always','tags')"
    )
    op.drop_column("messages", "mentions")
