"""elections, candidacies, ballots: the town council

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'elections',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('opens_tick', sa.Integer(), nullable=False),
        sa.Column('closes_tick', sa.Integer(), nullable=False, index=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='open'),
        sa.Column('winners', sa.JSON(), nullable=False, server_default='[]'),
    )
    op.create_table(
        'candidacies',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('election_id', sa.Integer(), sa.ForeignKey('elections.id'), nullable=False, index=True),
        sa.Column('agent_id', sa.Integer(), sa.ForeignKey('agents.id'), nullable=False),
        sa.Column('pitch', sa.UnicodeText(), nullable=False),
        sa.Column('tick', sa.Integer(), nullable=False),
        sa.UniqueConstraint('election_id', 'agent_id', name='uq_candidacy'),
    )
    op.create_table(
        'ballots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('election_id', sa.Integer(), sa.ForeignKey('elections.id'), nullable=False, index=True),
        sa.Column('voter_id', sa.Integer(), sa.ForeignKey('agents.id'), nullable=False),
        sa.Column('candidate_id', sa.Integer(), sa.ForeignKey('agents.id'), nullable=False),
        sa.Column('tick', sa.Integer(), nullable=False),
        sa.UniqueConstraint('election_id', 'voter_id', name='uq_ballot'),
    )


def downgrade() -> None:
    op.drop_table('ballots')
    op.drop_table('candidacies')
    op.drop_table('elections')
