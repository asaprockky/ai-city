"""Feelings: what a citizen feels, about whom, and why, as a real person would.

A feeling is an emotion with a strength (0-100), often aimed at someone, with a reason, kept on
the citizen (`Agent.feelings`). Feelings come from two places:

- what happens to them (`appraise`, called for every event): a gift brings gratitude, a theft
  that is caught brings anger, a death brings grief to those who cared, an election brings pride
  or disappointment, being fired brings anger and shame; and from their body each hour
  (`hourly`): starving brings fear, long solitude brings loneliness;
- what they feel about their own hour (`from_decision`): each decision may name up to three
  feelings, in character, which is where envy, love, guilt or hope usually come from.

Feelings fade by the hour, each at its own pace (irritation in hours, grief over weeks), pull the
mood up or down while they last, and are shown to the citizen (`describe`) so they colour what
they do and say. A citizen may act on a feeling, hide it or fight it, like anyone else.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.db.models import Agent, Event, Relationship

# emotion: (valence -1..1, kept per hour, Russian name for the channel)
EMOTIONS: dict[str, tuple[float, float, str]] = {
    "joy": (1.0, 0.94, "радость"),
    "gratitude": (0.8, 0.98, "благодарность"),
    "love": (1.0, 0.997, "любовь"),
    "affection": (0.7, 0.99, "теплота"),
    "pride": (0.8, 0.97, "гордость"),
    "hope": (0.6, 0.97, "надежда"),
    "relief": (0.6, 0.9, "облегчение"),
    "admiration": (0.5, 0.98, "восхищение"),
    "amusement": (0.6, 0.85, "веселье"),
    "anger": (-0.9, 0.92, "злость"),
    "resentment": (-0.7, 0.985, "обида"),
    "fear": (-0.8, 0.93, "страх"),
    "anxiety": (-0.6, 0.95, "тревога"),
    "sadness": (-0.7, 0.96, "грусть"),
    "grief": (-1.0, 0.995, "горе"),
    "disappointment": (-0.5, 0.96, "разочарование"),
    "shame": (-0.7, 0.97, "стыд"),
    "guilt": (-0.6, 0.98, "вина"),
    "jealousy": (-0.6, 0.98, "ревность"),
    "envy": (-0.5, 0.98, "зависть"),
    "loneliness": (-0.6, 0.96, "одиночество"),
    "contempt": (-0.6, 0.985, "презрение"),
    "disgust": (-0.6, 0.93, "отвращение"),
    "embarrassment": (-0.4, 0.85, "неловкость"),
    "boredom": (-0.3, 0.9, "скука"),
}
SYNONYMS = {"happiness": "joy", "happy": "joy", "thankful": "gratitude", "grateful": "gratitude",
            "angry": "anger", "rage": "anger", "fury": "anger", "irritation": "anger",
            "frustration": "anger", "afraid": "fear", "scared": "fear", "worry": "anxiety",
            "worried": "anxiety", "nervous": "anxiety", "sad": "sadness", "sorrow": "grief",
            "lonely": "loneliness", "ashamed": "shame", "guilty": "guilt", "jealous": "jealousy",
            "envious": "envy", "bitterness": "resentment", "spite": "resentment", "warmth": "affection",
            "fondness": "affection", "tenderness": "love", "respect": "admiration", "proud": "pride",
            "hopeful": "hope", "bored": "boredom", "scorn": "contempt", "humiliation": "shame"}
MAX_FEELINGS = 8
FADE_OUT = 8.0  # weaker than this is forgotten
MOOD_PULL = 2.5  # most a full-strength feeling moves the mood in an hour
STARVING, LONELY = 70.0, 15.0


def emotion_of(word: str) -> str | None:
    w = str(word or "").strip().lower()
    w = SYNONYMS.get(w, w)
    return w if w in EMOTIONS else None


def ru(emotion: str) -> str:
    return EMOTIONS.get(emotion, (0, 0, emotion))[2]


def feel(agent: Agent, emotion: str, strength: float, *, why: str, tick: int,
         toward: int | None = None, stir: bool = True) -> None:
    """Add a feeling, or stir one already there (the same emotion about the same person).
    With `stir` (something happened) it grows; without (the citizen names it again) it holds."""
    e = emotion_of(emotion)
    if e is None or not agent.alive or (toward is not None and toward == agent.id):
        return
    strength = max(0.0, min(100.0, float(strength)))
    out: list[dict[str, Any]] = [dict(f) for f in (agent.feelings or [])]
    for f in out:
        if f["e"] == e and f.get("to") == toward:
            old = float(f["s"])
            f["s"] = round(min(100.0, max(old, strength) + (0.25 * min(old, strength) if stir else 0)), 1)
            f["why"], f["t"] = why[:160], tick
            break
    else:
        out.append({"e": e, "to": toward, "s": round(strength, 1), "why": why[:160], "t": tick})
    out.sort(key=lambda f: -f["s"])
    agent.feelings = out[:MAX_FEELINGS]


def fade(agent: Agent, hours: int = 1) -> None:
    kept = []
    for f in agent.feelings or []:
        s = float(f["s"]) * EMOTIONS.get(f["e"], (0, 0.9, ""))[1] ** hours
        if s >= FADE_OUT:
            kept.append({**f, "s": round(s, 1)})
    agent.feelings = kept


def mood_pull(agent: Agent) -> float:
    """How much the citizen's feelings lift or sink their mood this hour."""
    pull = sum(EMOTIONS[f["e"]][0] * float(f["s"]) / 100 * MOOD_PULL
               for f in agent.feelings or [] if f["e"] in EMOTIONS)
    return max(-2 * MOOD_PULL, min(2 * MOOD_PULL, pull))


def hourly(agent: Agent, tick: int) -> None:
    """Fade the old feelings, add what the body says, and let them move the mood."""
    fade(agent)
    if agent.hunger >= STARVING and not agent.asleep:
        feel(agent, "fear", agent.hunger - 20, why="I am starving and do not know when I will eat", tick=tick)
    if agent.social <= LONELY and not agent.asleep:
        feel(agent, "loneliness", 50 - agent.social, why="I have not had a real talk with anyone in days",
             tick=tick)
    agent.mood = max(0.0, min(100.0, agent.mood + mood_pull(agent)))


SELF_REPORT_CAP = 75.0  # what one hour's mood can start at; only events push a feeling past it


def from_decision(session: Session, agent: Agent, reported: list[Any], tick: int,
                  perceived: set[int]) -> None:
    """The feelings a citizen names in their own decision (at most three, about people they see
    or know). Naming a feeling again keeps it alive but does not make it stronger."""
    known = perceived | {r.other_id for r in session.scalars(
        select(Relationship).where(Relationship.agent_id == agent.id))}
    for r in reported[:3]:
        toward = r.toward if isinstance(r.toward, int) and r.toward in known else None
        feel(agent, r.emotion, min(SELF_REPORT_CAP, r.strength), why=r.why or "", tick=tick,
             toward=toward, stir=False)


def _cares(session: Session, about: int, threshold: float = 0.2) -> list[tuple[int, float]]:
    return [(r.agent_id, r.affinity) for r in session.scalars(
        select(Relationship).where(Relationship.other_id == about, Relationship.affinity >= threshold))]


def appraise(session: Session, ev: Event) -> None:
    """Feelings that follow from what just happened, for those it happened to."""
    actor = session.get(Agent, ev.agent_id) if ev.agent_id else None
    target = session.get(Agent, ev.target_id) if ev.target_id else None
    d, t, k = ev.data or {}, ev.tick, ev.kind
    if k == "give" and actor and target:
        size = 25 + min(45, int(d.get("cents") or 0) / 20 + 10 * int(d.get("qty") or 0))
        feel(target, "gratitude", size, why=f"{actor.name} gave me something when I needed it", tick=t,
             toward=actor.id)
        feel(actor, "joy", 15, why=f"I could help {target.name}", tick=t)
    elif k == "steal" and actor and target and d.get("noticed"):
        feel(target, "anger", 75, why=f"{actor.name} stole from me", tick=t, toward=actor.id)
        feel(target, "fear", 35, why="my things are not safe here", tick=t)
        feel(actor, "shame", 30, why=f"{target.name} caught me stealing", tick=t)
    elif k == "steal" and target and not d.get("empty"):
        feel(target, "anxiety", 40, why="someone took my things and I do not know who", tick=t)
    elif k == "attack" and actor and target:
        feel(target, "anger", 80, why=f"{actor.name} hit me", tick=t, toward=actor.id)
        feel(target, "fear", 60, why=f"{actor.name} hit me", tick=t, toward=actor.id)
    elif k == "death" and actor:
        for who, aff in _cares(session, actor.id):
            mourner = session.get(Agent, who)
            if mourner is not None:
                feel(mourner, "grief", 50 + 50 * aff, why=f"{actor.name} is dead", tick=t, toward=actor.id)
        for other in session.scalars(select(Agent).where(Agent.alive.is_(True))):
            feel(other, "fear", 25, why=f"{actor.name} died ({d.get('cause', 'unknown')}); it could be me",
                 tick=t)
    elif k == "collapse" and actor:
        feel(actor, "fear", 45, why="my body gave way under me", tick=t)
    elif k == "fired" and actor and target:
        feel(target, "anger", 55, why=f"{actor.name} fired me", tick=t, toward=actor.id)
        feel(target, "shame", 35, why="I lost my job", tick=t)
    elif k == "hired" and actor:
        feel(actor, "relief", 35, why="I have paid work again", tick=t)
    elif k == "property" and actor:
        feel(actor, "pride", 60, why=f"I own {str(ev.summary).split(' bought ')[-1]}", tick=t)
    elif k == "candidacy" and actor:
        feel(actor, "hope", 45, why="I am standing for the town council", tick=t)
    elif k == "notice" and d.get("election") and "winners" in d:
        winners = set(d.get("winners") or [])
        for cid in (d.get("tally") or {}):
            who = session.get(Agent, int(cid))
            if who is None:
                continue
            if who.id in winners:
                feel(who, "pride", 70, why="the town elected me to the council", tick=t)
            else:
                feel(who, "disappointment", 55, why="the town did not elect me", tick=t)
    elif k == "notice" and d.get("author") == "AsapBank" and actor and d.get("loan_left"):
        feel(actor, "shame", 60, why="everyone knows I defaulted on my loan", tick=t)


def strength_word(s: float) -> str:
    return "overwhelming" if s >= 85 else "deep" if s >= 60 else "strong" if s >= 35 else "mild"


def describe(session: Session, agent: Agent) -> list[str]:
    """The citizen's feelings, strongest first, for their own perception."""
    from asapville.world.social import describe as name_of

    out = []
    for f in agent.feelings or []:
        whom = ""
        if f.get("to") is not None:
            other = session.get(Agent, int(f["to"]))
            if other is not None:
                whom = f" toward {name_of(session, agent.id, other)}"
        out.append(f"{f['e']}{whom} ({strength_word(float(f['s']))}): {f.get('why', '')}")
    return out


def summary_ru(session: Session, agent: Agent, limit: int = 3) -> str:
    """The strongest feelings in Russian, for the channel's pages."""
    parts = []
    for f in (agent.feelings or [])[:limit]:
        whom = ""
        if f.get("to") is not None and (other := session.get(Agent, int(f["to"]))) is not None:
            whom = f" ({other.name.split()[0]})"
        parts.append(f"{ru(f['e'])}{whom}")
    return ", ".join(parts)
