"""stalls: citizen-run businesses

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('stalls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('brand_id', sa.Integer(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.Unicode(length=128), nullable=False),
    sa.Column('item', sa.String(length=32), nullable=False),
    sa.Column('price_cents', sa.Integer(), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.Column('sold', sa.Integer(), nullable=False),
    sa.Column('opened_tick', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['brand_id'], ['brands.id'], name=op.f('fk_stalls_brand_id_brands')),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'], name=op.f('fk_stalls_location_id_locations')),
    sa.ForeignKeyConstraint(['owner_id'], ['agents.id'], name=op.f('fk_stalls_owner_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stalls')),
    sa.UniqueConstraint('owner_id', 'location_id', 'item', name='uq_stalls_triple')
    )
    with op.batch_alter_table('stalls', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stalls_location_id'), ['location_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stalls_owner_id'), ['owner_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('stalls', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stalls_owner_id'))
        batch_op.drop_index(batch_op.f('ix_stalls_location_id'))
    op.drop_table('stalls')
