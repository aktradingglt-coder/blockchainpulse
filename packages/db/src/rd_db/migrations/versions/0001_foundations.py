"""Foundations: TimescaleDB extension and service heartbeats.

Revision ID: 0001_foundations
Revises:
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_foundations"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table(
        "service_heartbeats",
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("service", "instance_id", name=op.f("pk_service_heartbeats")),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")
