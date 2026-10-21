"""bank_accounts: AsapBank savings and loans

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20 23:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bank_accounts',
        sa.Column('agent_id', sa.Integer(), sa.ForeignKey('agents.id'), primary_key=True),
        sa.Column('deposit_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('loan_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('loan_due_tick', sa.Integer(), nullable=True),
        sa.Column('defaulted', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('opened_tick', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_table('bank_accounts')
