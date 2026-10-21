"""feelings: what each citizen feels, about whom, and why

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agents') as batch:
        batch.add_column(sa.Column('feelings', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('agents') as batch:
        batch.drop_column('feelings')
