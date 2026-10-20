from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from asapville.db.models import Base
from asapville.db.session import upgrade_to_head


def test_migrations_match_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade_to_head(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models drifted from migrations: {diff}"
    tables = set(inspect(engine).get_table_names())
    assert {"agents", "locations", "events", "memories", "relationships", "llm_calls",
            "anomalies", "interventions", "brands", "world_state"} <= tables
