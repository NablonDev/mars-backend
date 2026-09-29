"""Extracted penalty rule revision table.

Revision ID: 763b4eb09a7d
Revises: e834e943318e
Create Date: 2026-09-23

Creates `penalties.extracted_penalty_rule_revision`: one row per
reviewer-requested re-extraction of a staged `extracted_penalty_rule`,
recording the reviewer's instruction, the agent's reply, and the
before/after attribute snapshots. `extracted_rule_id` FKs into
`penalties.extracted_penalty_rule.id` with `ondelete="CASCADE"` -- deleting
the staged rule deletes its revision history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "763b4eb09a7d"
down_revision: str | None = "e834e943318e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"

_JSONB_OR_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "extracted_penalty_rule_revision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extracted_rule_id", sa.Uuid(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("agent_reply", sa.Text(), nullable=True),
        sa.Column("before_snapshot", _JSONB_OR_JSON, nullable=False),
        sa.Column("after_snapshot", _JSONB_OR_JSON, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["extracted_rule_id"],
            [f"{PENALTIES}.extracted_penalty_rule.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extracted_rule_id", "revision_no", name="uq_extracted_penalty_rule_revision_rule_no"
        ),
        schema=PENALTIES,
    )
    op.create_index(
        "ix_extracted_penalty_rule_revision_extracted_rule_id",
        "extracted_penalty_rule_revision",
        ["extracted_rule_id"],
        unique=False,
        schema=PENALTIES,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extracted_penalty_rule_revision_extracted_rule_id",
        table_name="extracted_penalty_rule_revision",
        schema=PENALTIES,
    )
    op.drop_table("extracted_penalty_rule_revision", schema=PENALTIES)
