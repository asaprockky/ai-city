"""leads: the main heroes, who always think on the strong model

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agents') as batch:
        batch.add_column(sa.Column('lead', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('agents') as batch:
        batch.drop_column('lead')
