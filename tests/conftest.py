from __future__ import annotations

import pytest

from asapville.cli import init_world
from asapville.config import Settings, load_settings
from asapville.db.session import make_engine, make_session_factory
from asapville.engine import World
from asapville.llm.budget import BudgetGuard
from asapville.llm.client import LLMGateway
from asapville.llm.fake import FakeLLM


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = load_settings()
    s.db_url = f"sqlite:///{tmp_path / 'test.db'}"
    s.llm.backend = "fake"
    s.world.population = 5  # keep tests fast; config default is 10
    return s


@pytest.fixture
def sessions(settings):
    engine = make_engine(settings.db_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def fake() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def gateway(settings, fake) -> LLMGateway:
    guard = BudgetGuard(settings.budget, today=lambda: "2026-10-20",
                        opus_model=settings.llm.models.opus)
    return LLMGateway(fake, settings.llm, guard)


@pytest.fixture
async def world(settings, sessions, gateway) -> World:
    await init_world(settings, sessions=sessions, gateway=gateway)
    return World(settings, sessions, gateway)
