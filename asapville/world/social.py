"""Who knows whom. Names are learned by introduction, never read from the DB."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.agents.persona import short_looks
from asapville.agents.schemas import Personality
from asapville.db.models import Agent, Relationship


def get_relationship(session: Session, agent_id: int, other_id: int,
                     *, create: bool = False) -> Relationship | None:
    rel = session.scalar(select(Relationship).where(
        Relationship.agent_id == agent_id, Relationship.other_id == other_id))
    if rel is None and create:
        rel = Relationship(agent_id=agent_id, other_id=other_id, knows_name=False,
                           affinity=0.0, trust=0.0, debt_cents=0, opinion="")
        session.add(rel)
        session.flush()
    return rel


def knows_name(session: Session, observer_id: int, subject_id: int) -> bool:
    if observer_id == subject_id:
        return True
    rel = get_relationship(session, observer_id, subject_id)
    return bool(rel and rel.knows_name)


def looks_of(agent: Agent) -> str:
    try:
        return short_looks(Personality.model_validate(agent.personality))
    except Exception:  # malformed personality is an anomaly, not a crash
        return "a person you don't know"


def describe(session: Session, observer_id: int, subject: Agent) -> str:
    """How `observer` refers to `subject`: by name if introduced, else by looks."""
    if knows_name(session, observer_id, subject.id):
        return subject.name
    return f"a stranger ({looks_of(subject)})"


def introduce(session: Session, a: Agent, b: Agent, tick: int) -> None:
    """Talking face to face: both learn each other's name."""
    for x, y in ((a, b), (b, a)):
        rel = get_relationship(session, x.id, y.id, create=True)
        assert rel is not None
        rel.knows_name = True
        rel.last_interaction_tick = tick
