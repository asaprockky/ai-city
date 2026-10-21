"""world_state.wages: today's wage per job (the labour market)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('world_state', schema=None) as batch_op:
        batch_op.add_column(sa.Column('wages', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('world_state', schema=None) as batch_op:
        batch_op.drop_column('wages')
