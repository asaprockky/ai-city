"""democracy: proposals, votes, voted prices

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('world_state', schema=None) as batch_op:
        batch_op.add_column(sa.Column('prices', sa.JSON(), nullable=False, server_default='{}'))
    op.create_table('proposals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('closes_tick', sa.Integer(), nullable=False),
    sa.Column('text', sa.UnicodeText(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('target', sa.String(length=32), nullable=True),
    sa.Column('value_cents', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('yes', sa.Integer(), nullable=False),
    sa.Column('no', sa.Integer(), nullable=False),
    sa.Column('decided_tick', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['author_id'], ['agents.id'], name=op.f('fk_proposals_author_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_proposals'))
    )
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_proposals_author_id'), ['author_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_proposals_closes_tick'), ['closes_tick'], unique=False)
    op.create_table('votes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=False),
    sa.Column('yes', sa.Boolean(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_votes_agent_id_agents')),
    sa.ForeignKeyConstraint(['proposal_id'], ['proposals.id'], name=op.f('fk_votes_proposal_id_proposals')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_votes')),
    sa.UniqueConstraint('proposal_id', 'agent_id', name='uq_votes_pair')
    )
    with op.batch_alter_table('votes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_votes_proposal_id'), ['proposal_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('votes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_votes_proposal_id'))
    op.drop_table('votes')
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_proposals_closes_tick'))
        batch_op.drop_index(batch_op.f('ix_proposals_author_id'))
    op.drop_table('proposals')
    with op.batch_alter_table('world_state', schema=None) as batch_op:
        batch_op.drop_column('prices')
