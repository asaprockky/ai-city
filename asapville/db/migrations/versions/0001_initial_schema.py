"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-20 05:33:14.080416
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('locations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('name', sa.Unicode(length=128), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('x', sa.Integer(), nullable=False),
    sa.Column('y', sa.Integer(), nullable=False),
    sa.Column('capacity', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('condition', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_locations')),
    sa.UniqueConstraint('key', name=op.f('uq_locations_key'))
    )
    with op.batch_alter_table('locations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_locations_kind'), ['kind'], unique=False)

    op.create_table('world_state',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('mode', sa.String(length=32), nullable=False),
    sa.Column('speed', sa.Float(), nullable=False),
    sa.Column('weather', sa.String(length=32), nullable=False),
    sa.Column('temperature_c', sa.Float(), nullable=False),
    sa.Column('seed', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_world_state'))
    )
    op.create_table('agents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.Unicode(length=128), nullable=False),
    sa.Column('tier', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.Column('seed_card', sa.JSON(), nullable=False),
    sa.Column('personality', sa.JSON(), nullable=False),
    sa.Column('virtual_mac', sa.String(length=17), nullable=False),
    sa.Column('virtual_ip', sa.String(length=15), nullable=False),
    sa.Column('employer_id', sa.Integer(), nullable=True),
    sa.Column('home_id', sa.Integer(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('destination_id', sa.Integer(), nullable=True),
    sa.Column('travel_ticks_left', sa.Integer(), nullable=False),
    sa.Column('money_cents', sa.Integer(), nullable=False),
    sa.Column('inventory', sa.JSON(), nullable=False),
    sa.Column('health', sa.Float(), nullable=False),
    sa.Column('hunger', sa.Float(), nullable=False),
    sa.Column('energy', sa.Float(), nullable=False),
    sa.Column('social', sa.Float(), nullable=False),
    sa.Column('mood', sa.Float(), nullable=False),
    sa.Column('mood_label', sa.Unicode(length=64), nullable=False),
    sa.Column('reputation', sa.JSON(), nullable=False),
    sa.Column('bedtime_hour', sa.Integer(), nullable=False),
    sa.Column('asleep', sa.Boolean(), nullable=False),
    sa.Column('alive', sa.Boolean(), nullable=False),
    sa.Column('born_tick', sa.Integer(), nullable=False),
    sa.Column('died_tick', sa.Integer(), nullable=True),
    sa.Column('cause_of_death', sa.Unicode(length=256), nullable=True),
    sa.Column('last_action', sa.String(length=32), nullable=True),
    sa.Column('last_action_args', sa.JSON(), nullable=False),
    sa.Column('last_thought', sa.UnicodeText(), nullable=True),
    sa.ForeignKeyConstraint(['destination_id'], ['locations.id'], name=op.f('fk_agents_destination_id_locations')),
    sa.ForeignKeyConstraint(['employer_id'], ['agents.id'], name=op.f('fk_agents_employer_id_agents')),
    sa.ForeignKeyConstraint(['home_id'], ['locations.id'], name=op.f('fk_agents_home_id_locations')),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'], name=op.f('fk_agents_location_id_locations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agents')),
    sa.UniqueConstraint('virtual_ip', name=op.f('uq_agents_virtual_ip')),
    sa.UniqueConstraint('virtual_mac', name=op.f('uq_agents_virtual_mac'))
    )
    op.create_table('anomalies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('severity', sa.Integer(), nullable=False),
    sa.Column('detail', sa.UnicodeText(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('fix_attempts', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_anomalies_agent_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_anomalies'))
    )
    with op.batch_alter_table('anomalies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_anomalies_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_anomalies_tick'), ['tick'], unique=False)

    op.create_table('brands',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.Unicode(length=128), nullable=False),
    sa.Column('category', sa.String(length=48), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('founded_tick', sa.Integer(), nullable=False),
    sa.Column('logo_description', sa.UnicodeText(), nullable=False),
    sa.Column('reputation', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['agents.id'], name=op.f('fk_brands_owner_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_brands')),
    sa.UniqueConstraint('name', name=op.f('uq_brands_name'))
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=True),
    sa.Column('target_id', sa.Integer(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('summary', sa.UnicodeText(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('visibility', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_events_agent_id_agents')),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'], name=op.f('fk_events_location_id_locations')),
    sa.ForeignKeyConstraint(['target_id'], ['agents.id'], name=op.f('fk_events_target_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events'))
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_events_agent_id'), ['agent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_tick'), ['tick'], unique=False)

    op.create_table('llm_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('real_day', sa.String(length=10), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=True),
    sa.Column('angel_role', sa.String(length=16), nullable=True),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('backend', sa.String(length=16), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('tier', sa.Integer(), nullable=False),
    sa.Column('effort', sa.String(length=16), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error', sa.UnicodeText(), nullable=True),
    sa.Column('reason', sa.UnicodeText(), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_llm_calls_agent_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_calls'))
    )
    with op.batch_alter_table('llm_calls', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_calls_agent_id'), ['agent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_llm_calls_real_day'), ['real_day'], unique=False)

    op.create_table('relationships',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=False),
    sa.Column('other_id', sa.Integer(), nullable=False),
    sa.Column('knows_name', sa.Boolean(), nullable=False),
    sa.Column('affinity', sa.Float(), nullable=False),
    sa.Column('trust', sa.Float(), nullable=False),
    sa.Column('debt_cents', sa.Integer(), nullable=False),
    sa.Column('last_interaction_tick', sa.Integer(), nullable=True),
    sa.Column('opinion', sa.UnicodeText(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_relationships_agent_id_agents')),
    sa.ForeignKeyConstraint(['other_id'], ['agents.id'], name=op.f('fk_relationships_other_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_relationships')),
    sa.UniqueConstraint('agent_id', 'other_id', name='uq_relationships_pair')
    )
    with op.batch_alter_table('relationships', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_relationships_agent_id'), ['agent_id'], unique=False)

    op.create_table('interventions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tick', sa.Integer(), nullable=False),
    sa.Column('anomaly_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('target', sa.String(length=128), nullable=False),
    sa.Column('before', sa.JSON(), nullable=False),
    sa.Column('after', sa.JSON(), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('reason', sa.UnicodeText(), nullable=False),
    sa.Column('applied', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['anomaly_id'], ['anomalies.id'], name=op.f('fk_interventions_anomaly_id_anomalies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interventions'))
    )
    op.create_table('memories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=False),
    sa.Column('created_at_tick', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('content', sa.UnicodeText(), nullable=False),
    sa.Column('importance', sa.Integer(), nullable=False),
    sa.Column('embedding', sa.JSON(), nullable=True),
    sa.Column('source_event_id', sa.Integer(), nullable=True),
    sa.Column('last_accessed_tick', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_memories_agent_id_agents')),
    sa.ForeignKeyConstraint(['source_event_id'], ['events.id'], name=op.f('fk_memories_source_event_id_events')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memories'))
    )
    with op.batch_alter_table('memories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_memories_agent_id'), ['agent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_memories_created_at_tick'), ['created_at_tick'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('memories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_memories_created_at_tick'))
        batch_op.drop_index(batch_op.f('ix_memories_agent_id'))

    op.drop_table('memories')
    op.drop_table('interventions')
    with op.batch_alter_table('relationships', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_relationships_agent_id'))

    op.drop_table('relationships')
    with op.batch_alter_table('llm_calls', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_llm_calls_real_day'))
        batch_op.drop_index(batch_op.f('ix_llm_calls_agent_id'))

    op.drop_table('llm_calls')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_events_tick'))
        batch_op.drop_index(batch_op.f('ix_events_kind'))
        batch_op.drop_index(batch_op.f('ix_events_agent_id'))

    op.drop_table('events')
    op.drop_table('brands')
    with op.batch_alter_table('anomalies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_anomalies_tick'))
        batch_op.drop_index(batch_op.f('ix_anomalies_kind'))

    op.drop_table('anomalies')
    op.drop_table('agents')
    op.drop_table('world_state')
    with op.batch_alter_table('locations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_locations_kind'))

    op.drop_table('locations')
