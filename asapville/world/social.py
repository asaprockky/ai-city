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


# --------------------------------------------------------------------------- reciprocity
HISTORY = 6  # deeds remembered per relationship


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, v))


def record_deed(session: Session, observer_id: int, actor: Agent, tick: int, text: str, *,
                trust: float = 0.0, affinity: float = 0.0) -> None:
    """`observer` remembers what `actor` did to them (or before their eyes): it moves trust and liking."""
    if observer_id == actor.id:
        return
    rel = get_relationship(session, observer_id, actor.id, create=True)
    assert rel is not None
    rel.trust = _clamp(rel.trust + trust)
    rel.affinity = _clamp(rel.affinity + affinity)
    rel.last_interaction_tick = tick
    sign = 1 if trust + affinity > 0 else -1 if trust + affinity < 0 else 0
    rel.history = (list(rel.history or []) + [{"tick": tick, "did": text, "sign": sign}])[-HISTORY:]


def reputation(agent: Agent) -> float:
    return float((agent.reputation or {}).get("town", 0.0))


def nudge_reputation(agent: Agent, delta: float) -> None:
    rep = dict(agent.reputation or {})
    rep["town"] = round(_clamp(rep.get("town", 0.0) + delta), 3)
    agent.reputation = rep


def _words(trust: float, affinity: float) -> str:
    t = ("you trust them" if trust > 0.35 else "you somewhat trust them" if trust > 0.1
         else "you don't trust them at all" if trust < -0.35 else "you are wary of them" if trust < -0.1
         else "")
    a = ("you like them" if affinity > 0.3 else "you resent them" if affinity < -0.3 else "")
    return "; ".join(x for x in (t, a) if x)


def standing(session: Session, agent: Agent, *, limit: int = 6) -> list[str]:
    """How this citizen stands with the people they know, with what those people did to them."""
    rels = [r for r in session.scalars(select(Relationship).where(Relationship.agent_id == agent.id))
            if r.history or abs(r.trust) > 0.1 or abs(r.affinity) > 0.1]
    rels.sort(key=lambda r: (abs(r.trust) + abs(r.affinity), r.last_interaction_tick or 0), reverse=True)
    out = []
    for r in rels[:limit]:
        other = session.get(Agent, r.other_id)
        if other is None:
            continue
        who = other.name if r.knows_name else f"a stranger ({looks_of(other)})"
        if not other.alive:
            who += " (dead)"
        deeds = "; ".join(h["did"] for h in (r.history or [])[-3:])
        feel = _words(r.trust, r.affinity)
        out.append(who + (f": {feel}" if feel else "") + (f". They: {deeds}" if deeds else ""))
    return out
