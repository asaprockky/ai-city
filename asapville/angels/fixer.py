"""The Fixer: an angel that reads the Watcher's findings and intervenes, within limits.

One LLM call per round (Sonnet; Opus for severity >= 4 or after two failed rounds).
Every intervention is validated, capped by config, written as an `Intervention`
row, and shown on the dashboard. Citizens only ever see the in-world effect: a
notice on the board, a cart at the shop, a job offer, a relief parcel.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from asapville.config import Settings
from asapville.db.models import Agent, Anomaly, Intervention, Stall, WorldState
from asapville.llm.budget import BudgetExceeded
from asapville.llm.client import LLMGateway
from asapville.llm.routing import ANGEL, AngelTask, effort_for, model_for
from asapville.llm.types import ChatMessage, LLMRequest
from asapville.world import economy
from asapville.world.actions import TickContext
from asapville.world.events import emit, remember

MAX_INTERVENTIONS = 6

FIXER_RULES = """You are the Fixer, a guardian angel of Asapville: a simulated small town whose \
citizens are LLM-driven people with real needs, real money and permanent death. You belong to the \
simulation layer. Citizens never see you; they only see effects in their world.

The Watcher has flagged problems. Keep the town alive with the LIGHTEST touch that works, so \
that the citizens' own choices decide what happens. The town's fate is theirs: shortages, \
failures and bad choices are part of their story. Prefer telling them what is true over doing \
anything for them.

What you can do:
{tools}
Citizens are free. Never assign anyone a job or tell people how to live; a notice may say \
what is for sale, what work is open and what it pays, and they choose.

Rules: at most {{max_n}} interventions. Do not fix what citizens can fix themselves within a few \
hours. The dead cannot be helped; a notice may mourn or warn. Never invent places, items or \
people that are not in the status report.

Reply with ONLY a JSON object:
{{{{"assessment": "<2-3 sentences: what is wrong and why>",
  "interventions": [{{{{"kind": "<{kinds}>", ..., "why": "<one sentence>"}}}}]}}}}"""

TOOLS = {
    "notice": 'notice {{"text": "..."}}: pin a notice on the town board, signed "the town council". '
              'Everyone reads it for a day. Be concrete: name the place, the price, the job, the wage.',
    "restock": 'restock {{"location": "<place key>", "item": "...", "qty": N}}: a cart delivers goods '
               'to a shelf (max {max_restock}). Only when nobody in town can make or fetch it.',
    "grant": 'grant {{"agent_id": N, "cents": N}}: relief money from the town fund (max {max_grant} cents).',
    "give_food": 'give_food {{"agent_id": N, "item": "...", "qty": N}}: a food parcel from the town '
                 'stores (max {max_food}). ONLY for someone who will die within hours otherwise.',
}

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class FixerReply(BaseModel):
    assessment: str = Field(max_length=2000)
    interventions: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class FixReport:
    ok: bool
    assessment: str = ""
    applied: list[Intervention] = field(default_factory=list)
    note: str = ""


def rules(settings: Settings) -> str:
    a = settings.angels
    allowed = [k for k in TOOLS if k in a.allow]
    tools = "".join(f"- {TOOLS[k]}\n" for k in allowed)
    return FIXER_RULES.format(tools=tools, kinds="|".join(allowed)).format(
        max_restock=a.max_restock, max_grant=a.max_grant_cents, max_food=a.max_food,
        max_n=MAX_INTERVENTIONS)


def parse_reply(text: str) -> FixerReply:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    try:
        return FixerReply.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc)[:300]) from exc


def town_status(ctx: TickContext, agents: list[Agent]) -> str:
    """Omniscient status for the angel: clock, fund, shelves, jobs, every citizen."""
    s, cfg = ctx.session, ctx.settings
    ws = s.get(WorldState, 1)
    assert ws is not None
    stocks = economy.stocks_by_location(s, ctx.locations)
    lines = [f"Time: {ctx.time.label()} ({ctx.time.part_of_day}); weather {ctx.weather}.",
             f"Town fund: {ws.treasury_cents / 100:.2f} asaps."]
    lines.append("Shelves (place key: goods):")
    for key in sorted(set(cfg.economy.shops) | set(cfg.economy.stock_start)):
        loc = ctx.by_key.get(key)
        if loc is None:
            continue
        goods = ", ".join(f"{k} {v}" for k, v in sorted(stocks.get(key, {}).items())
                          if k not in cfg.economy.unlimited)
        always = [k for k in cfg.economy.shops.get(key, []) if k in cfg.economy.unlimited]
        if always:
            goods = (goods + "; " if goods else "") + "always available: " + ", ".join(always)
        goods = goods or "empty"
        sells = cfg.economy.shops.get(key)
        tail = f" (sells {', '.join(sells)})" if sells else ""
        lines.append(f"  - {key} ({loc.name}): {goods}{tail}")
    lines.append("Prices: " + ", ".join(f"{k} {v / 100:.2f}" for k, v in cfg.economy.prices.items()))
    lines.append("Jobs (job: place, wage/h, who):")
    for name, job in cfg.economy.jobs.items():
        who = ", ".join(a.name for a in economy.workers_in(s, name)) or "nobody"
        lines.append(f"  - {name}: {job.workplace}, {job.wage_cents / 100:.2f}, {who}")
    stalls = list(s.scalars(select(Stall).order_by(Stall.id)))
    if stalls:
        lines.append("Citizens' own businesses: " + "; ".join(
            f'"{st.name}" ({s.get(Agent, st.owner_id).name}) at {ctx.locations[st.location_id].key}: '
            f"{st.item} {st.price_cents / 100:.2f}, {st.qty} left" for st in stalls))
    lines.append("Citizens (id, name, job, where, hunger/energy/health, money, carrying, mood, thought):")
    for a in agents:
        if not a.alive:
            lines.append(f"  - {a.id} {a.name}: DEAD ({a.cause_of_death})")
            continue
        where = ctx.locations[a.location_id].name if a.location_id else (
            f"walking to {ctx.locations[a.destination_id].name}" if a.destination_id else "?")
        inv = ", ".join(f"{k} x{v}" for k, v in (a.inventory or {}).items()) or "nothing"
        thought = (a.last_thought or "").replace("\n", " ")[:140]
        state = "asleep" if a.asleep else (a.last_action or "idle")
        lines.append(f"  - {a.id} {a.name}: {a.role}, {where} ({state}), "
                     f"{a.hunger:.0f}/{a.energy:.0f}/{a.health:.0f}, {a.money_cents / 100:.2f}, "
                     f"{inv}, {a.mood_label}, \"{thought}\"")
    return "\n".join(lines)


def _apply(ctx: TickContext, iv: dict[str, Any], model: str, anomaly_id: int | None) -> Intervention:
    s, cfg = ctx.session, ctx.settings
    kind = str(iv.get("kind", "")).strip().lower()
    why = str(iv.get("why", ""))[:500]
    row = Intervention(tick=ctx.tick, anomaly_id=anomaly_id, kind="state_patch", target=kind,
                       before={}, after={}, model=model, reason=why, applied=False)
    s.add(row)
    ws = s.get(WorldState, 1)
    assert ws is not None

    def agent_of() -> Agent:
        a = s.get(Agent, int(iv.get("agent_id")))  # type: ignore[arg-type]
        if a is None or not a.alive:
            raise ValueError("no such living citizen")
        return a

    try:
        if kind not in cfg.angels.allow:
            raise ValueError("not allowed: the citizens decide this themselves")
        if kind == "notice":
            text = str(iv.get("text", "")).strip()[:400]
            if not text:
                raise ValueError("empty notice")
            emit(s, tick=ctx.tick, kind="notice", visibility="public",
                 summary=f'Town notice: "{text}"', data={"text": text, "author": "the town council"})
            row.target, row.after = "notice", {"text": text}
        elif kind == "restock":
            loc = ctx.by_key.get(str(iv.get("location", "")))
            item, qty = str(iv.get("item", "")), int(iv.get("qty", 0))
            if loc is None or not item or not (0 < qty <= cfg.angels.max_restock):
                raise ValueError("bad place, item or quantity")
            before = economy.stock_qty(s, loc.id, item)
            after = economy.stock_add(s, loc.id, item, qty)
            emit(s, tick=ctx.tick, kind="delivery", location_id=loc.id,
                 summary=f"A cart delivered {qty} {item} to {loc.name}", data={"item": item, "qty": qty},
                 witness_memory=f"A cart arrived and unloaded {qty} {item} here.", importance=2)
            row.target = f"stock:{loc.key}:{item}"
            row.before, row.after = {"qty": before}, {"qty": after}
        elif kind == "appoint":
            raise ValueError("citizens choose their own work")
        elif kind == "grant":
            a = agent_of()
            cents = int(iv.get("cents", 0))
            if not (0 < cents <= cfg.angels.max_grant_cents):
                raise ValueError("bad amount")
            if ws.treasury_cents < cents:
                raise ValueError("the town fund cannot cover it")
            row.target = f"agent:{a.id}:money"
            row.before, row.after = {"money_cents": a.money_cents}, {"money_cents": a.money_cents + cents}
            a.money_cents += cents
            ws.treasury_cents -= cents
            emit(s, tick=ctx.tick, kind="relief", actor=a, location_id=a.location_id, visibility="private",
                 summary=f"The council granted {a.name} {cents / 100:.2f} asaps of relief",
                 data={"cents": cents},
                 actor_memory=f"The town council granted me {cents / 100:.2f} asaps of relief money.",
                 importance=4)
        elif kind == "give_food":
            a = agent_of()
            item, qty = str(iv.get("item", "")), int(iv.get("qty", 0))
            if item not in cfg.food or item in cfg.economy.served or not (0 < qty <= cfg.angels.max_food):
                raise ValueError("bad item or quantity")
            inv = dict(a.inventory or {})
            row.target, row.before = f"agent:{a.id}:inventory", {"inventory": dict(inv)}
            inv[item] = inv.get(item, 0) + qty
            a.inventory = inv
            row.after = {"inventory": dict(inv)}
            emit(s, tick=ctx.tick, kind="relief", actor=a, location_id=a.location_id, visibility="private",
                 summary=f"The council handed {a.name} a parcel of {qty} {item}",
                 data={"item": item, "qty": qty},
                 actor_memory=f"Someone from the town stores handed me a parcel: {qty} {item}. "
                              f"I should eat.", importance=5)
        else:
            raise ValueError(f"unknown intervention {kind!r}")
        row.applied = True
    except (ValueError, TypeError, KeyError) as exc:
        row.reason = f"REJECTED ({exc}): {why}"
        row.applied = False
    s.flush()
    return row


async def run(ctx: TickContext, gateway: LLMGateway, anomalies: list[Anomaly],
              agents: list[Agent] | None = None) -> FixReport:
    s, cfg = ctx.session, ctx.settings
    if agents is None:
        agents = list(s.scalars(select(Agent).order_by(Agent.id)))
    severity = max(a.severity for a in anomalies)
    failed = max(a.fix_attempts for a in anomalies)
    task = AngelTask("fixer", severity=severity, failed_fixes=failed)
    model = model_for(task, cfg.llm.models)
    prompt = (town_status(ctx, agents) + "\n\nOpen anomalies (id, severity 1-5, kind, detail):\n"
              + "\n".join(f"  - #{a.id} sev {a.severity} {a.kind}: {a.detail}" for a in anomalies)
              + "\n\nWhat, if anything, do you do? Reply with only the JSON object.")
    unemployed = [a.id for a in agents if a.alive and a.role not in cfg.economy.jobs]
    req = LLMRequest(
        purpose="fixer", model=model, tier=ANGEL, effort=effort_for(task, cfg.llm.effort),
        system_rules=rules(cfg), messages=[ChatMessage(role="user", content=prompt)],
        max_tokens=cfg.angels.max_tokens, angel_role="fixer",
        reason=f"fixer: {len(anomalies)} open anomalies, worst severity {severity}, "
               f"{failed} failed rounds",
        context={"tick": ctx.tick, "unemployed": unemployed, "jobs": list(cfg.economy.jobs),
                 "anomalies": [{"id": a.id, "kind": a.kind, "severity": a.severity,
                                "agent_id": a.agent_id, "data": a.data} for a in anomalies]},
    )
    try:
        result = await gateway.call(req, tick=ctx.tick)
    except BudgetExceeded as exc:
        return FixReport(ok=False, note=f"budget: {exc.scope}")
    except Exception as exc:  # recorded in llm_calls; try again next round
        for a in anomalies:
            a.fix_attempts += 1
        return FixReport(ok=False, note=f"llm error: {type(exc).__name__}")
    try:
        reply = parse_reply(result.text)
    except ValueError as exc:
        for a in anomalies:
            a.fix_attempts += 1
        s.add(Anomaly(tick=ctx.tick, kind="fixer_parse_failure", severity=2, source="fixer",
                      detail=f"Fixer reply unreadable: {exc}", data={"text": result.text[:1000]}))
        return FixReport(ok=False, note=f"unreadable reply: {exc}")
    first = anomalies[0].id
    applied = [_apply(ctx, iv, model, first) for iv in reply.interventions[:MAX_INTERVENTIONS]]
    for a in anomalies:
        a.status = "fixed"
        a.fix_attempts += 1
    done = [f"{r.target}" + ("" if r.applied else " (rejected)") for r in applied]
    emit(s, tick=ctx.tick, kind="angel", visibility="system",
         summary=f"Fixer ({model}): {reply.assessment}",
         data={"model": model, "anomalies": [a.id for a in anomalies], "interventions": done,
               "assessment": reply.assessment})
    for a in agents:  # the dashboard's "angel" tab reads interventions; citizens only see effects
        if a.alive and any(r.applied and r.target == "notice" for r in applied):
            remember(s, a.id, ctx.tick, "observation", "A new notice went up on the town board.",
                     importance=2)
    return FixReport(ok=True, assessment=reply.assessment, applied=applied)
