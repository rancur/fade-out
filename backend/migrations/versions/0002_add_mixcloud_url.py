"""add mixes.mixcloud_url

Adds the ``mixcloud_url`` column introduced alongside the (default-OFF) Mixcloud
upload path. Uses a batch ALTER so it also works on SQLite.

Revision ID: 0002_add_mixcloud_url
Revises: 0001_initial_schema
Create Date: 2026-07-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_add_mixcloud_url"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("mixes") as batch_op:
        batch_op.add_column(sa.Column("mixcloud_url", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mixes") as batch_op:
        batch_op.drop_column("mixcloud_url")
