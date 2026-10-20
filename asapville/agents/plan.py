"""The morning plan: once a day each citizen decides, in their own words, what the day is for.

One short LLM call per citizen per simulated day, on their first waking turn. The plan is
theirs alone (a private memory), shown back to them every hour of that day, so a life is
more than the next meal: ambitions, people, schemes, things they want to make.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from asapville.agents.schemas import Perception
from asapville.llm.budget import BudgetExceeded
from asapville.llm.client import LLMGateway
from asapville.llm.types import ChatMessage, LLMRequest
from asapville.world.perception import render_perception

PLAN_RULES = """You are role-playing one citizen of Asapville, a small town. You are that person, \
not an assistant. It is the start of your day. Nobody tells you how to live: you can work a \
job or not, make things, trade, help or exploit people, court someone, start a venture, \
organise the town, pursue an old dream or a new one. Eating and sleeping matter, but they \
are not what a life is for.

Decide what YOU want from today, guided by your nature, ambitions, fears, the people you \
have met and what you see the town lacks. Be concrete and in character: places, people, \
things to make or do. Two to four short steps, in your own voice.

Reply with ONLY a JSON object: {"plan": "<your plan for today>", "mood": "<one or two words>"}"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class PlanReply(BaseModel):
    plan: str = Field(min_length=3, max_length=800)
    mood: str = Field(default="calm", max_length=64)


def parse_plan(text: str) -> PlanReply:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    try:
        return PlanReply.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc)[:300]) from exc


async def plan_day(*, gateway: LLMGateway, agent_id: int, tier: int, model: str, effort: str | None,
                   persona: str, perception: Perception, tick: int,
                   max_tokens: int = 1024) -> PlanReply | None:
    """The citizen's own plan for today, or None if the call failed (they just live the day)."""
    prompt = (render_perception(perception)
              + "\n\nWhat do you want to do with today? Reply with only the JSON.")
    req = LLMRequest(
        purpose="plan", model=model, tier=tier, effort=effort, system_rules=PLAN_RULES,
        system_persona=persona, messages=[ChatMessage(role="user", content=prompt)],
        max_tokens=max_tokens, agent_id=agent_id,
        context={"tick": tick, "perception": perception.model_dump()},
    )
    try:
        result = await gateway.call(req, tick=tick)
        return parse_plan(result.text)
    except (BudgetExceeded, ValueError):
        return None
    except Exception:  # API trouble is recorded in llm_calls; the day goes on without a plan
        return None
