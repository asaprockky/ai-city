"""Direct democracy at the Town Hall: any citizen proposes, everyone may vote, the majority rules.

A proposal is a law (words only, the town's record), a wage for a town job, or a shelf price
for a town shop. It stays open for VOTING_HOURS; it passes when more citizens vote yes than no
and at least QUORUM vote at all. A passed wage or price changes the world at once.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.config import Settings
from asapville.db.models import Agent, Proposal, Vote, WorldState
from asapville.world.events import emit

VOTING_HOURS = 12
QUORUM = 3
WAGE_RANGE = (50, 1500)  # cents per hour a vote may set
PRICE_RANGE = (10, 10_000)  # cents per item a vote may set
VOTING_PLACES = ("town_hall", "square")


def open_proposals(session: Session) -> list[Proposal]:
    return list(session.scalars(select(Proposal).where(Proposal.status == "open").order_by(Proposal.id)))


def recent_decisions(session: Session, limit: int = 3) -> list[Proposal]:
    return list(session.scalars(select(Proposal).where(Proposal.status != "open")
                                .order_by(Proposal.decided_tick.desc(), Proposal.id.desc()).limit(limit)))


def parse_change(settings: Settings, change: Any) -> tuple[str, str | None, int | None] | str:
    """(kind, target, cents) for a proposal's `change`, or an error in words."""
    if not change:
        return ("law", None, None)
    if not isinstance(change, dict):
        return 'A change looks like {"wage": "farmer", "to": 4.0} or {"price": "bread", "to": 2.5}.'
    try:
        cents = round(float(change.get("to")) * 100)
    except (TypeError, ValueError):
        return 'Say what it should become: "to": <asaps>.'
    if "wage" in change:
        job = str(change["wage"]).strip().lower()
        if job not in settings.economy.jobs:
            return f"There is no town job called '{job}'."
        if not WAGE_RANGE[0] <= cents <= WAGE_RANGE[1]:
            return f"A wage must be between {WAGE_RANGE[0] / 100:.2f} and {WAGE_RANGE[1] / 100:.2f} asaps."
        return ("wage", job, cents)
    if "price" in change:
        item = str(change["price"]).strip().lower()
        if item not in settings.economy.prices:
            return f"The town shops don't sell '{item}'."
        if not PRICE_RANGE[0] <= cents <= PRICE_RANGE[1]:
            return f"A price must be between {PRICE_RANGE[0] / 100:.2f} and {PRICE_RANGE[1] / 100:.2f} asaps."
        return ("price", item, cents)
    return 'A change is either {"wage": <job>, "to": <asaps>} or {"price": <item>, "to": <asaps>}.'


def describe(p: Proposal) -> str:
    change = ""
    if p.kind == "wage":
        change = f" [sets the {p.target} wage to {p.value_cents / 100:.2f}/h]"
    elif p.kind == "price":
        change = f" [sets the {p.target} price to {p.value_cents / 100:.2f}]"
    return f'#{p.id} "{p.text}"{change}'


def propose(session: Session, settings: Settings, author: Agent, text: str, change: Any,
            tick: int, location_id: int | None) -> Proposal:
    parsed = parse_change(settings, change)
    assert not isinstance(parsed, str)
    kind, target, cents = parsed
    p = Proposal(author_id=author.id, tick=tick, closes_tick=tick + VOTING_HOURS, text=text[:300],
                 kind=kind, target=target, value_cents=cents, status="open", yes=0, no=0)
    session.add(p)
    session.flush()
    emit(session, tick=tick, kind="proposal", actor=author, location_id=location_id, visibility="public",
         summary=f"{author.name} proposed {describe(p)}",
         data={"proposal": p.id, "text": p.text, "kind": kind, "target": target, "value": cents,
               "closes": p.closes_tick},
         actor_memory=f"I proposed to the town {describe(p)}. Voting is open for {VOTING_HOURS} hours.",
         witness_memory=f"{{actor}} put a proposal to the town: {describe(p)}.", importance=5)
    return p


def cast(session: Session, voter: Agent, p: Proposal, yes: bool, tick: int,
         location_id: int | None) -> None:
    v = session.scalar(select(Vote).where(Vote.proposal_id == p.id, Vote.agent_id == voter.id))
    if v is None:
        session.add(Vote(proposal_id=p.id, agent_id=voter.id, yes=yes, tick=tick))
    else:
        v.yes, v.tick = yes, tick
    session.flush()
    votes = list(session.scalars(select(Vote).where(Vote.proposal_id == p.id)))
    p.yes = sum(1 for x in votes if x.yes)
    p.no = len(votes) - p.yes
    emit(session, tick=tick, kind="vote", actor=voter, location_id=location_id, visibility="local",
         summary=f"{voter.name} voted {'yes' if yes else 'no'} on #{p.id} ({p.yes} yes, {p.no} no)",
         data={"proposal": p.id, "yes": yes, "tally": [p.yes, p.no]},
         actor_memory=f"I voted {'yes' if yes else 'no'} on {describe(p)}.",
         witness_memory=f"{{actor}} cast a vote on proposal #{p.id}.", importance=3)


def close_due(session: Session, tick: int) -> list[Proposal]:
    """Decide every proposal whose voting time is over; apply the ones that pass."""
    done = []
    for p in session.scalars(select(Proposal).where(Proposal.status == "open", Proposal.closes_tick <= tick)):
        passed = p.yes > p.no and p.yes + p.no >= QUORUM
        p.status, p.decided_tick = ("passed" if passed else "failed"), tick
        ws = session.get(WorldState, 1)
        if passed and ws is not None and p.kind == "wage":
            ws.wages = {**(ws.wages or {}), p.target: p.value_cents}
        elif passed and ws is not None and p.kind == "price":
            ws.prices = {**(ws.prices or {}), p.target: p.value_cents}
        verdict = "PASSED" if passed else ("failed" if p.yes + p.no >= QUORUM else "failed: too few votes")
        author = session.get(Agent, p.author_id)
        emit(session, tick=tick, kind="law", visibility="public",
             summary=f"The town voted {p.yes}-{p.no} on {describe(p)}: {verdict}",
             data={"proposal": p.id, "passed": passed, "yes": p.yes, "no": p.no, "text": p.text,
                   "kind": p.kind, "target": p.target, "value": p.value_cents,
                   "author": author.name if author else None,
                   "notice": f"The town voted {p.yes} to {p.no} on {describe(p)}: {verdict}."},
             importance=5)
        done.append(p)
    session.flush()
    return done
