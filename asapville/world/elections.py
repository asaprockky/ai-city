"""The town council: elected by everyone, three seats, one week at a time.

An election opens whenever no council sits (at the start, and when a term ends). For a day
any citizen may stand (`stand`, with a pitch to the town) and every citizen may cast one
ballot for one candidate (`elect`), from wherever they are. When it closes, the three with
the most votes take the seats for a week. While a council sits, proposals at the Town Hall
are decided by it: two councillors voting yes pass a proposal, two voting no sink it.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.db.models import Agent, Ballot, Candidacy, Election
from asapville.world.events import emit

SEATS = 3
ELECTION_HOURS = 24
TERM_HOURS = 7 * 24
MAJORITY = SEATS // 2 + 1


def open_election(session: Session) -> Election | None:
    return session.scalar(select(Election).where(Election.status == "open").order_by(Election.id.desc()))


def sitting(session: Session, tick: int) -> Election | None:
    """The election whose winners sit on the council now, if any."""
    e = session.scalar(select(Election).where(Election.status == "decided").order_by(Election.id.desc()))
    if e is None or tick >= e.closes_tick + TERM_HOURS:
        return None
    return e


def council(session: Session, tick: int) -> list[Agent]:
    e = sitting(session, tick)
    if e is None:
        return []
    return [a for a in (session.get(Agent, i) for i in e.winners or []) if a is not None and a.alive]


def candidates(session: Session, e: Election) -> list[Candidacy]:
    return list(session.scalars(select(Candidacy).where(Candidacy.election_id == e.id)
                                .order_by(Candidacy.id)))


def tally(session: Session, e: Election) -> Counter[int]:
    return Counter(b.candidate_id for b in session.scalars(select(Ballot).where(Ballot.election_id == e.id)))


def stand(session: Session, e: Election, agent: Agent, pitch: str, tick: int,
          location_id: int | None) -> None:
    session.add(Candidacy(election_id=e.id, agent_id=agent.id, pitch=pitch[:300], tick=tick))
    session.flush()
    emit(session, tick=tick, kind="candidacy", actor=agent, location_id=location_id, visibility="public",
         summary=f'{agent.name} is standing for the town council: "{pitch[:300]}"',
         data={"election": e.id, "pitch": pitch[:300],
               "notice": f'{agent.name} stands for the town council: "{pitch[:200]}" Vote with elect.'},
         actor_memory=f'I stood for the town council. My pitch: "{pitch[:300]}"', importance=7)


def cast(session: Session, e: Election, voter: Agent, candidate: Agent, tick: int) -> None:
    b = session.scalar(select(Ballot).where(Ballot.election_id == e.id, Ballot.voter_id == voter.id))
    if b is None:
        session.add(Ballot(election_id=e.id, voter_id=voter.id, candidate_id=candidate.id, tick=tick))
    else:
        b.candidate_id, b.tick = candidate.id, tick
    session.flush()
    emit(session, tick=tick, kind="ballot", actor=voter, visibility="private",
         summary=f"{voter.name} voted for {candidate.name} in the council election",
         data={"election": e.id, "candidate": candidate.id},
         actor_memory=f"I cast my ballot for {candidate.name} for the town council.", importance=4)


def step(session: Session, tick: int) -> None:
    """Open an election when no council sits, and decide one whose day is over."""
    e = open_election(session)
    if e is not None:
        if tick >= e.closes_tick:
            _decide(session, e, tick)
        return
    if sitting(session, tick) is None:
        e = Election(opens_tick=tick, closes_tick=tick + ELECTION_HOURS, status="open", winners=[])
        session.add(e)
        session.flush()
        text = (f"Election for the town council: {SEATS} seats for one week. Anyone may stand (stand, with a "
                f"pitch) and everyone may vote once (elect) for the next {ELECTION_HOURS} hours. The council "
                "decides the town's proposals and may spend the town fund on public works.")
        emit(session, tick=tick, kind="notice", visibility="public", summary=text,
             data={"text": text, "author": "the town clerk", "election": e.id}, importance=6)


def _decide(session: Session, e: Election, tick: int) -> None:
    votes = tally(session, e)
    order = {c.agent_id: c.id for c in candidates(session, e)}
    ranked = sorted((a for a in order if votes.get(a, 0) > 0), key=lambda a: (-votes[a], order[a]))
    winners = ranked[:SEATS]
    e.status, e.winners = "decided", winners
    if not winners:  # nobody stood, or nobody voted: try again tomorrow
        e.closes_tick = tick - TERM_HOURS
        text = "The council election drew no votes; another opens now."
    else:
        names = ", ".join(f"{session.get(Agent, a).name} ({votes[a]})" for a in winners)
        text = (f"The town elected its council: {names}. They sit for a week and decide the town's "
                "proposals: two councillors' yes passes one.")
    emit(session, tick=tick, kind="notice", visibility="public", summary=text,
         data={"text": text, "author": "the town clerk", "election": e.id, "winners": winners,
               "tally": dict(votes)}, importance=7)
    for a in winners:
        agent = session.get(Agent, a)
        if agent is not None:
            from asapville.world.events import remember

            remember(session, a, tick, "outcome", f"I was elected to the town council with {votes[a]} votes. "
                     "For a week, my vote decides the town's proposals, with the other councillors.",
                     importance=8)
    session.flush()


def describe(session: Session, agent: Agent, tick: int) -> list[str]:
    """Lines for a citizen's perception about the council and any election under way."""
    out = []
    members = council(session, tick)
    if members:
        e = sitting(session, tick)
        ends = (e.closes_tick + TERM_HOURS - tick) if e else 0
        names = ", ".join(m.name for m in members)
        mine = " You are on it." if any(m.id == agent.id for m in members) else ""
        out.append(f"Town council: {names} (for {ends} more hours); two councillors' yes passes a proposal."
                   + mine)
    e = open_election(session)
    if e is not None:
        votes = tally(session, e)
        cands = candidates(session, e)
        mine = session.scalar(select(Ballot).where(Ballot.election_id == e.id, Ballot.voter_id == agent.id))
        listed = "; ".join(
            f'{session.get(Agent, c.agent_id).name} (ref {c.agent_id}, {votes.get(c.agent_id, 0)} votes): '
            f'"{c.pitch[:120]}"' for c in cands) or "nobody yet"
        out.append(f"COUNCIL ELECTION, closes in {e.closes_tick - tick} hours. Candidates: {listed}. "
                   + ("You have voted." if mine else 'Vote from anywhere: elect {"candidate": <ref>}.')
                   + ("" if any(c.agent_id == agent.id for c in cands)
                      else ' Or stand yourself: stand {"pitch": "<why the town should pick you>"}.'))
    return out
