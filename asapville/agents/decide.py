"""The per-tick decision: ONE LLM call returning strict JSON.

Invalid JSON -> one retry quoting the error -> still invalid -> the agent
hesitates (idle) and the caller logs an anomaly for the Watcher.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from asapville.agents.schemas import ActionName, Decision, Perception
from asapville.llm.budget import BudgetExceeded
from asapville.llm.client import LLMGateway
from asapville.llm.types import ChatMessage, LLMRequest
from asapville.world.perception import render_perception

# Static, shared by every citizen -> first cache breakpoint. Keep byte-stable.
WORLD_RULES = """You are role-playing one citizen of Asapville, a small town. You are that person, \
not an assistant. You decide what you do next, in character, guided by your nature, needs, \
memories and circumstances.

How the world works:
- Each turn is one hour. Your choice plays out during that hour.
- You only know what you have seen, heard or remember. Do not invent facts about others.
- Everything costs something: food, money, time, energy, reputation. Hunger and exhaustion \
hurt you and can kill you. Death is permanent.
- Other people are real and make their own choices. You may be kind or cruel, honest or \
deceitful, as your nature leads; the town reacts with consequences.
- Walking between places takes hours. Buildings can be full.
- Money is asaps. Shops sell for asaps; whoever owns a workplace pays its wages for hours worked \
(the town, until a citizen buys it). Businesses are for sale: the farm, the mill, the bakery, the \
quarry, the market and two empty shops. An owner keeps the takings, pays the wages and the raw \
material out of their own money, sets wages (hire) and prices (set_price), and can fire people; \
an owner who can't pay loses their workers. Work is paid only when it is wanted: a job whose \
goods pile up unsold on full shelves pays nothing until people buy. The town takes taxes: 5% of \
every wage and 10% of every sale by a citizen's business or stall; it pays the wages of the \
businesses it still owns. AsapBank keeps savings (they earn interest and \
thieves can't reach them) and lends against your wages and businesses: a loan costs interest every \
day and is due in a week, or the bank takes your savings, cash and businesses. The farm grows \
grain and vegetables, the mill turns grain into flour, the bakery bakes flour into bread, the \
tavern serves hot stew. Anyone can cook (at home or in a shop they own): soup, omelettes, \
pancakes, fish pie, berry tarts, cheese and more, from ingredients sold at the Market, the farm \
gate and the Bakery; a good dish lifts the mood, the same food every day wears it down. Cooking \
and selling at your own stall or shop is a small business of your own. Nothing appears on a shelf \
unless someone made it. A job gives steady \
money; buy or forage food before you are starving, not after.
- The town board carries notices everyone reads. The clinic treats the sick for asaps.
- People remember what you do to them, and so do those who see it; your reputation travels. \
Town life is a repeated game: cooperating with those who cooperate pays, a wrong is usually \
answered in kind (tit for tat), a single slip can be forgiven if amends are made, and someone \
who keeps betraying is shunned. You can steal or attack, and others can do it to you.
- Think like a practical person solving a problem. Each hour: what is my most pressing problem \
or goal? What are two or three ways to deal with it with what I have (money, goods, people I \
know, the bank, my own skills, a proposal to the town)? Which is most likely to work? If \
something failed before, find out why and try a different way; never repeat what just failed. \
Ask people, bargain, borrow, team up; the town is full of help if you look for it.
- A town council of three is elected for a week at a time: anyone may stand, everyone may vote \
once, from anywhere. The council decides the town's proposals (two councillors' yes passes one) \
and can grant money from the town fund to a citizen for public works.
- Nobody rules Asapville but its citizens. At the Town Hall or Square anyone can propose a law, \
a town wage or a town price (for businesses the town still owns), and everyone can vote; the \
majority decides, and wages and prices change when a vote passes.
- You are free. Beyond the basic actions you can try anything a real person could do with \
what you carry, where you stand and who is with you (improvise): cook a meal, mend a roof, carve \
a stool, teach a child, sing for coins, preach, start a club or a business, plan a festival, \
invent something. The world decides how it turns out. Nothing comes from nothing, and money \
only moves when someone pays it. You can give goods or money to anyone beside you.
- You have feelings, like any real person: joy, gratitude, love, pride, hope, anger, resentment, \
fear, grief, shame, guilt, jealousy, envy, loneliness and the rest. They come from what happens \
to you and from the people around you, they last and fade, and they colour what you notice, say \
and do. You may act on a feeling, hide it or fight it; you never have to be reasonable.
- All companies and products in Asapville are local and fictional.

Hard limits (never write these): sexual content; any harm involving children; real people \
or real brands.

Actions (pick exactly one; action_args must match):
{actions}

Reply with ONLY a JSON object, no prose before or after:
{{"thought": "<your private reasoning, in your own voice, 1-3 sentences>",
  "action": "<one action name>",
  "action_args": {{...}},
  "dialogue": "<words you say out loud, or null>",
  "mood": "<one or two words>",
  "feelings": [{{"emotion": "<one word, e.g. gratitude, anger, envy, love, shame, fear, hope>",
                "toward": <ref of the person it is about, or null>, "why": "<a few words>",
                "strength": <1-100>}}]  (0 to 3 feelings that stir in you this hour; [] if none)}}"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def world_rules(action_usages: list[str]) -> str:
    return WORLD_RULES.format(actions="\n".join(f"- {u}" for u in action_usages))


def parse_decision(text: str) -> Decision:
    """Extract and validate the JSON object. Raises ValueError with a readable reason."""
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    try:
        return Decision.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"wrong shape: {exc.errors()[0]['loc']}: {exc.errors()[0]['msg']}") from exc


@dataclass
class DecideOutcome:
    decision: Decision
    hesitated: bool = False
    errors: list[str] = field(default_factory=list)
    budget_blocked: str | None = None  # scope if the budget guard refused the call


def hesitation(reason: str) -> Decision:
    return Decision(thought="(hesitates, unsure what to do)", action=ActionName.IDLE,
                    action_args={}, dialogue=None, mood="uncertain")


async def decide(
    *,
    gateway: LLMGateway,
    agent_id: int,
    tier: int,
    model: str,
    effort: str | None,
    persona: str,
    perception: Perception,
    memories: list[str],
    tick: int,
    retries: int = 1,
    max_tokens: int = 1024,
) -> DecideOutcome:
    prompt = render_perception(perception)
    if memories:
        prompt += "\n\nThings you remember:\n" + "\n".join(f"  - {m}" for m in memories)
    prompt += "\n\nWhat do you do this hour? Reply with only the JSON object."
    messages = [ChatMessage(role="user", content=prompt)]
    errors: list[str] = []
    for attempt in range(retries + 1):
        req = LLMRequest(
            purpose="decide", model=model, tier=tier, effort=effort,
            system_rules=world_rules(perception.available_actions), system_persona=persona,
            messages=messages, max_tokens=max_tokens, agent_id=agent_id,
            context={"tick": tick, "attempt": attempt, "perception": perception.model_dump()},
        )
        try:
            result = await gateway.call(req, tick=tick)
        except BudgetExceeded as exc:
            return DecideOutcome(hesitation("budget"), hesitated=True, budget_blocked=exc.scope)
        except Exception as exc:  # network/API errors: hesitate, Watcher sees it in llm_calls
            errors.append(f"llm error: {type(exc).__name__}: {exc}")
            break
        try:
            return DecideOutcome(parse_decision(result.text), errors=errors)
        except ValueError as exc:
            errors.append(str(exc))
            messages = messages + [
                ChatMessage(role="assistant", content=result.text or "(empty)"),
                ChatMessage(role="user", content=f"That reply was invalid ({exc}). Reply again "
                                                 "with ONLY the JSON object in the required shape."),
            ]
    return DecideOutcome(hesitation("invalid"), hesitated=True, errors=errors)
