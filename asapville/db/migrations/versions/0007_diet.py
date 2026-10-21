"""agents.diet: what each citizen ate lately (for taste and boredom)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('diet', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.drop_column('diet')
