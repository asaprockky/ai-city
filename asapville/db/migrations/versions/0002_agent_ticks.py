"""agent_ticks: per-tick snapshots for the dashboard timeline

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agent_ticks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=False),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('destination_id', sa.Integer(), nullable=True),
    sa.Column('travel_ticks_left', sa.Integer(), nullable=False),
    sa.Column('asleep', sa.Boolean(), nullable=False),
    sa.Column('alive', sa.Boolean(), nullable=False),
    sa.Column('hunger', sa.Float(), nullable=False),
    sa.Column('energy', sa.Float(), nullable=False),
    sa.Column('social', sa.Float(), nullable=False),
    sa.Column('health', sa.Float(), nullable=False),
    sa.Column('mood', sa.Float(), nullable=False),
    sa.Column('mood_label', sa.Unicode(length=64), nullable=False),
    sa.Column('money_cents', sa.Integer(), nullable=False),
    sa.Column('inventory', sa.JSON(), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=True),
    sa.Column('thought', sa.UnicodeText(), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_agent_ticks_agent_id_agents')),
    sa.ForeignKeyConstraint(['destination_id'], ['locations.id'],
                            name=op.f('fk_agent_ticks_destination_id_locations')),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'],
                            name=op.f('fk_agent_ticks_location_id_locations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_ticks')),
    sa.UniqueConstraint('tick', 'agent_id', name='uq_agent_ticks_pair')
    )
    with op.batch_alter_table('agent_ticks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_ticks_agent_id'), ['agent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_ticks_tick'), ['tick'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('agent_ticks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_ticks_tick'))
        batch_op.drop_index(batch_op.f('ix_agent_ticks_agent_id'))
    op.drop_table('agent_ticks')
