"""The Judge: an angel that decides how citizens' free-form attempts turn out.

Citizens may `improvise` anything a real person could do. Once per tick, after
everyone has acted, ONE LLM call (Sonnet) judges every attempt of that hour
together. Its verdicts are bounded in code: goods only from what the citizen
used (or the land they stand on), nourishment never grows from nothing, money
never appears, nobody else's belongings change. Citizens see only the outcome.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from asapville.agents.schemas import Personality
from asapville.db.models import Agent
from asapville.llm.budget import BudgetExceeded
from asapville.llm.client import LLMGateway
from asapville.llm.routing import ANGEL, AngelTask, effort_for, model_for
from asapville.llm.types import ChatMessage, LLMRequest
from asapville.world import needs as needs_rules
from asapville.world.actions import TickContext
from asapville.world.events import emit

MAX_MADE = 3  # units of goods one attempt can produce
WILD_KINDS = {"forest", "river", "farm", "quarry"}  # places that give raw material
WILD_NOURISHMENT = 15.0  # what the land itself can add to a meal made there
_ITEM = re.compile(r"[^a-z' -]+")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

JUDGE_RULES = """You are the Judge, a guardian angel of Asapville: a simulated small town whose \
citizens are LLM-driven people with real needs, money and permanent death. You belong to the \
simulation layer; citizens never see you.

This hour some citizens tried something of their own invention. Decide, realistically and \
fairly, what happens to each attempt, given who they are, where they stand, what they carry, \
the time and the weather.
- Ordinary things usually work. Ambitious things take many hours and may only partly work this \
hour (say how far they got). Impossible or absurd things fail, gently.
- Nothing comes from nothing. Goods come from the materials the citizen listed in "uses", or \
from the land where they stand (forest: wood, herbs, berries; river: fish, reeds, clay; farm: \
straw, eggs; quarry: stone). Money is never created. Other people's belongings never change.
- Food they make must be one of: {foods}. A meal cannot nourish more than what went into it. \
Never produce: {forbidden}.
- Keep it grounded and specific. Let character show: a cruel person's prank, a kind person's \
help, a vain person's showing off.

For each attempt return:
  agent_id, success (true/false),
  outcome: one or two sentences in the present tense, what an onlooker sees. Write {{actor}} \
where the person's name would go (and {{target}} for the person they did it with, if any),
  memory: one first-person sentence they will remember,
  energy_cost: 0-20, consume: {{item: qty}} (only from their "uses"), produce: {{item: qty}} \
(at most {max_made} units in total, plain lowercase names such as "wooden stool"),
  mood: -10..10, health: -5..5,
  notice: text for the town board, or null (only if they wrote or posted something public).

Reply with ONLY a JSON object: {{"results": [ ... one object per attempt ... ]}}"""


class Verdict(BaseModel):
    agent_id: int
    success: bool = False
    outcome: str = Field(default="", max_length=600)
    memory: str = Field(default="", max_length=400)
    energy_cost: float = 5
    consume: dict[str, int] = Field(default_factory=dict)
    produce: dict[str, int] = Field(default_factory=dict)
    mood: float = 0
    health: float = 0
    notice: str | None = None


def rules(ctx: TickContext) -> str:
    cfg = ctx.settings
    foods = ", ".join(k for k in cfg.food if k not in cfg.economy.unlimited)
    forbidden = ", ".join(cfg.economy.unlimited) + ", money, asaps, coins"
    return JUDGE_RULES.format(foods=foods, forbidden=forbidden, max_made=MAX_MADE)


def parse_reply(text: str) -> list[Verdict]:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    try:
        data = json.loads(cleaned[start:end + 1])
        return [Verdict.model_validate(r) for r in data.get("results", [])]
    except (json.JSONDecodeError, ValidationError, AttributeError) as exc:
        raise ValueError(str(exc)[:300]) from exc


def _clean_item(name: str) -> str:
    return re.sub(r"\s+", " ", _ITEM.sub("", str(name).lower())).strip()[:32]


def describe_attempt(ctx: TickContext, a: dict[str, Any]) -> str:
    s = ctx.session
    agent = s.get(Agent, a["agent_id"])
    assert agent is not None
    p = Personality.model_validate(agent.personality)
    here = ctx.locations.get(a["location_id"])
    other = s.get(Agent, a["with_id"]) if a.get("with_id") else None
    traits = ", ".join(f"{k} {v:.1f}" for k, v in {**p.big_five.model_dump(),
                                                   **p.dark_traits.model_dump()}.items())
    inv = ", ".join(f"{k} x{v}" for k, v in (agent.inventory or {}).items()) or "nothing"
    return "\n".join([
        f"- agent_id {agent.id}: {agent.name}, {agent.seed_card.get('age', '?')} years old, "
        f"{p.moral_alignment}; {traits}. Job: {agent.role}. Quirks: {'; '.join(p.quirks) or '-'}.",
        f"  At {here.name if here else '?'} ({here.kind if here else '?'}); energy {agent.energy:.0f}, "
        f"health {agent.health:.0f}, hunger {agent.hunger:.0f}; carries {inv}; "
        f"{agent.money_cents / 100:.2f} asaps.",
        f"  Tries: \"{a['what']}\"",
        f"  Uses: {json.dumps(a['uses']) if a['uses'] else 'nothing'}"
        + (f"; together with {other.name} (agent_id {other.id})" if other else ""),
    ])


def _apply(ctx: TickContext, a: dict[str, Any], v: Verdict | None) -> None:
    s, cfg = ctx.session, ctx.settings
    agent = s.get(Agent, a["agent_id"])
    if agent is None or not agent.alive:
        return
    other = s.get(Agent, a["with_id"]) if a.get("with_id") else None
    here = ctx.locations.get(a["location_id"])
    if v is None:
        v = Verdict(agent_id=agent.id, success=False, energy_cost=3,
                    outcome=f"{{actor}} tries to {a['what'][:120]}, but nothing much comes of it.",
                    memory=f"I tried to {a['what'][:120]}, but nothing much came of it.")
    inv = dict(agent.inventory or {})
    consumed: dict[str, int] = {}
    for item, qty in v.consume.items():
        item = _clean_item(item)
        q = min(int(qty), a["uses"].get(item, 0), inv.get(item, 0))
        if q > 0:
            consumed[item] = q
            inv[item] -= q
            if inv[item] <= 0:
                del inv[item]
    budget = sum(cfg.food[i].nutrition * q for i, q in consumed.items() if i in cfg.food) * 1.25
    if here is not None and here.kind in WILD_KINDS:
        budget += WILD_NOURISHMENT
    made: dict[str, int] = {}
    room = MAX_MADE
    for item, qty in v.produce.items():
        item = _clean_item(item)
        q = max(0, min(int(qty), room))
        if not item or q == 0 or item in cfg.economy.unlimited or item in ("asaps", "money", "coins"):
            continue
        if item in cfg.food:
            q = min(q, int(budget // max(1.0, cfg.food[item].nutrition)))
            budget -= q * cfg.food[item].nutrition
        if q > 0:
            made[item] = made.get(item, 0) + q
            inv[item] = inv.get(item, 0) + q
            room -= q
    agent.inventory = inv
    agent.energy = needs_rules.clamp(agent.energy - max(0.0, min(20.0, v.energy_cost)))
    agent.mood = needs_rules.clamp(agent.mood + max(-10.0, min(10.0, v.mood)))
    agent.health = needs_rules.clamp(agent.health + max(-5.0, min(5.0, v.health)))
    outcome = v.outcome.strip() or ("{actor} " + a["what"][:200])
    if "{actor}" not in outcome:
        outcome = outcome.replace(agent.name, "{actor}", 1)
    if other is not None and "{target}" not in outcome:
        outcome = outcome.replace(other.name, "{target}", 1)
    summary = outcome.replace("{actor}", agent.name).replace("{target}", other.name if other else "someone")
    emit(s, tick=ctx.tick, kind="improvise", actor=agent, target=other, location_id=a["location_id"],
         summary=summary, data={"what": a["what"], "success": v.success, "consumed": consumed,
                                "made": made, "outcome": summary},
         actor_memory=v.memory.strip() or f"I tried to {a['what'][:150]}.",
         target_memory=outcome if other else None, witness_memory=outcome,
         importance=4 if v.success else 3)
    if v.notice and v.notice.strip():
        text = v.notice.strip()[:300]
        emit(s, tick=ctx.tick, kind="notice", actor=agent, location_id=a["location_id"],
             visibility="public", summary=f'{agent.name} posted a notice: "{text}"',
             data={"text": text, "author_id": agent.id}, importance=3)


async def run(ctx: TickContext, gateway: LLMGateway, attempts: list[dict[str, Any]]) -> int:
    """Judge every attempt of this tick in one call. Returns how many were judged by the model."""
    if not attempts:
        return 0
    cfg = ctx.settings
    task = AngelTask("judge")
    model = model_for(task, cfg.llm.models)
    prompt = (f"Time: {ctx.time.label()} ({ctx.time.part_of_day}); weather {ctx.weather}.\n"
              "Attempts this hour:\n" + "\n".join(describe_attempt(ctx, a) for a in attempts)
              + "\n\nJudge each attempt. Reply with only the JSON object.")
    req = LLMRequest(
        purpose="judge", model=model, tier=ANGEL, effort=effort_for(task, cfg.llm.effort),
        system_rules=rules(ctx), messages=[ChatMessage(role="user", content=prompt)],
        max_tokens=cfg.angels.max_tokens, angel_role="judge",
        context={"tick": ctx.tick, "attempts": attempts},
    )
    verdicts: dict[int, Verdict] = {}
    try:
        result = await gateway.call(req, tick=ctx.tick)
        verdicts = {v.agent_id: v for v in parse_reply(result.text)}
    except (BudgetExceeded, ValueError):
        pass  # every attempt falls back to "nothing much comes of it"
    except Exception:  # API trouble is recorded in llm_calls; the hour still passes
        pass
    for a in attempts:
        _apply(ctx, a, verdicts.get(a["agent_id"]))
    return len(verdicts)
