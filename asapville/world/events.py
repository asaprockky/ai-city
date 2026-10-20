"""World events: persisted rows, witness memories and a live pub/sub feed.

Visibility:
- public  : the whole town eventually hears (deaths, elections)
- local   : seen by awake agents at the same location
- private : only the actor (and target) know
- system  : simulation layer (angels, budget). NEVER becomes a citizen memory.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.db.models import Agent, Event, Memory

PERCEIVABLE = {"public", "local", "private"}


class EventBus:
    """In-process fan-out for the dashboard WebSocket (Phase 7)."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, payload: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            if q.full():
                q.get_nowait()  # drop oldest for slow consumers
            q.put_nowait(payload)


def event_payload(e: Event) -> dict[str, Any]:
    return {"id": e.id, "tick": e.tick, "kind": e.kind, "agent_id": e.agent_id,
            "target_id": e.target_id, "location_id": e.location_id, "summary": e.summary,
            "visibility": e.visibility, "data": e.data}


def witnesses(session: Session, location_id: int | None, *, exclude: set[int]) -> list[Agent]:
    """Awake, living agents physically at the location."""
    if location_id is None:
        return []
    q = select(Agent).where(Agent.location_id == location_id, Agent.alive.is_(True),
                            Agent.asleep.is_(False), Agent.travel_ticks_left == 0)
    return [a for a in session.scalars(q) if a.id not in exclude]


def remember(session: Session, agent_id: int, tick: int, kind: str, content: str, *,
             importance: int = 3, event_id: int | None = None) -> Memory:
    m = Memory(agent_id=agent_id, created_at_tick=tick, kind=kind, content=content,
               importance=importance, source_event_id=event_id)
    session.add(m)
    return m


def emit(
    session: Session,
    *,
    tick: int,
    kind: str,
    summary: str,
    actor: Agent | None = None,
    target: Agent | None = None,
    location_id: int | None = None,
    visibility: str = "local",
    data: dict[str, Any] | None = None,
    actor_memory: str | None = None,
    target_memory: str | None = None,
    witness_memory: str | None = None,
    memory_kind: str = "observation",
    importance: int = 3,
) -> Event:
    """Persist an event and write the memories of everyone who could perceive it.

    Memory texts may contain `{actor}` / `{target}`; they are filled in from each
    observer's point of view (name if introduced, appearance otherwise).
    `summary` is the omniscient observer-dashboard text and is never shown to citizens.
    """
    from asapville.world.social import describe

    ev = Event(tick=tick, kind=kind, summary=summary,
               agent_id=actor.id if actor else None, target_id=target.id if target else None,
               location_id=location_id, visibility=visibility, data=data or {})
    session.add(ev)
    session.flush()
    from asapville.world import feelings

    feelings.appraise(session, ev)
    if visibility not in PERCEIVABLE:
        return ev

    def fmt(template: str, observer_id: int) -> str:
        # str.replace, not str.format: memory text may quote dialogue containing braces.
        out = template
        if "{actor}" in out:
            out = out.replace("{actor}", describe(session, observer_id, actor) if actor else "someone")
        if "{target}" in out:
            out = out.replace("{target}", describe(session, observer_id, target) if target else "someone")
        return out

    if actor is not None and actor_memory:
        remember(session, actor.id, tick, "action", fmt(actor_memory, actor.id),
                 importance=importance, event_id=ev.id)
    if target is not None and target_memory:
        remember(session, target.id, tick, memory_kind, fmt(target_memory, target.id),
                 importance=importance, event_id=ev.id)
    if visibility == "local" and witness_memory:
        skip = {a.id for a in (actor, target) if a is not None}
        for w in witnesses(session, location_id, exclude=skip):
            remember(session, w.id, tick, memory_kind, fmt(witness_memory, w.id),
                     importance=importance, event_id=ev.id)
    return ev
