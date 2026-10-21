"""relationships.history: what each citizen remembers the other did to them

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20 19:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('relationships', schema=None) as batch_op:
        batch_op.add_column(sa.Column('history', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('relationships', schema=None) as batch_op:
        batch_op.drop_column('history')
