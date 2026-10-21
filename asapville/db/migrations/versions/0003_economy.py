"""economy: stocks, world_ticks, treasury

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('stocks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('location_id', sa.Integer(), nullable=False),
    sa.Column('item', sa.String(length=32), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'], name=op.f('fk_stocks_location_id_locations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stocks')),
    sa.UniqueConstraint('location_id', 'item', name='uq_stocks_pair')
    )
    with op.batch_alter_table('stocks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stocks_location_id'), ['location_id'], unique=False)

    op.create_table('world_ticks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('treasury_cents', sa.Integer(), nullable=False),
    sa.Column('stocks', sa.JSON(), nullable=False),
    sa.Column('weather', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_world_ticks')),
    sa.UniqueConstraint('tick', name=op.f('uq_world_ticks_tick'))
    )
    with op.batch_alter_table('world_state', schema=None) as batch_op:
        batch_op.add_column(sa.Column('treasury_cents', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('world_state', schema=None) as batch_op:
        batch_op.drop_column('treasury_cents')
    op.drop_table('world_ticks')
    with op.batch_alter_table('stocks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stocks_location_id'))
    op.drop_table('stocks')
