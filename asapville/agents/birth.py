"""Two-step birth: the world rolls a seed card, the agent writes itself.

The world decides the facts of birth (age, family, wealth, temperament, moral
baseline). The agent's own model then chooses its name, look, voice, values,
fears, ambitions, secrets and backstory. The world only validates the format.
"""

from __future__ import annotations

import json
import random

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.agents.schemas import BigFive, DarkTraits, Personality, SeedCard, SelfPortrait
from asapville.config import Settings
from asapville.db.models import Agent, Location
from asapville.llm.client import LLMGateway
from asapville.llm.routing import effort_for, model_for, tier_for
from asapville.llm.types import ChatMessage, LLMRequest

BIRTHPLACES = ["Asapville", "Asapville", "Asapville", "the hill farms", "a river barge",
               "a distant city", "a mining camp", "the coast", "an orphanage two valleys over"]
FORMATIVE_EVENTS = [
    "lost a parent young", "was the favourite child", "grew up hungry", "was bullied for years",
    "won a prize that made them proud", "saw a friend drown in the river",
    "was cheated out of an inheritance", "was raised by strict grandparents",
    "ran away from home at fifteen", "nursed a sibling through a long illness",
    "was caught stealing and publicly shamed", "survived a house fire", "fell in love and was betrayed",
    "was taught a trade by a kind master", "watched their family business go bankrupt",
    "was the only one in the family who learned to read",
]
WEALTH = [("poor", 0.35), ("modest", 0.40), ("comfortable", 0.20), ("wealthy", 0.05)]
WEALTH_CENTS = {"poor": (500, 3000), "modest": (3000, 15000), "comfortable": (15000, 60000),
                "wealthy": (60000, 200000)}
WEALTH_BREAD = {"poor": (0, 2), "modest": (1, 3), "comfortable": (2, 4), "wealthy": (3, 5)}
DARK_ALIGNMENTS = ("selfish", "cruel", "criminal")
DARK_SHARE = 0.15

BIRTH_RULES = """You are about to become a person in Asapville, a small fictional town. \
The world has already decided the facts of your birth (the seed card). Everything else about \
you is YOUR choice: your name, how you look and dress, how you speak, what you value and fear, \
what you want, what you hide, and your story. Make yourself a specific, believable, flawed \
human being consistent with the seed card, including its darker traits if any. \
No real people, no real brands, nothing sexual, no harm involving children.

Reply with ONLY a JSON object of this shape:
{"name": "<first and last name>",
 "values": ["..."], "fears": ["..."], "ambitions": ["..."], "secrets": ["..."],
 "speech_style": "<how you talk, concretely>",
 "quirks": ["..."],
 "appearance": {"hair": "...", "build": "...", "clothing": "...", "signature_item": "...",
                "self_description": "<one sentence, how a stranger would see you>"},
 "backstory": "<3-5 sentences>"}"""


def _weighted(rng: random.Random, options: list[tuple[str, float]]) -> str:
    return rng.choices([o for o, _ in options], weights=[w for _, w in options])[0]


def _trait(rng: random.Random, mean: float = 0.5, sd: float = 0.2) -> float:
    return round(min(1.0, max(0.0, rng.gauss(mean, sd))), 2)


def roll_seed_card(rng: random.Random, settings: Settings, *, force_dark: bool = False) -> SeedCard:
    dark = force_dark or rng.random() < DARK_SHARE
    alignment = rng.choice(DARK_ALIGNMENTS) if dark else _weighted(
        rng, [("lawful-good", 0.4), ("neutral", 0.6)])
    big_five = BigFive(
        openness=_trait(rng), conscientiousness=_trait(rng), extraversion=_trait(rng),
        agreeableness=_trait(rng, 0.25 if dark else 0.55), neuroticism=_trait(rng),
    )
    dark_traits = DarkTraits(
        machiavellianism=_trait(rng, 0.7 if dark else 0.25),
        narcissism=_trait(rng, 0.65 if dark else 0.3),
        psychopathy=_trait(rng, 0.6 if dark else 0.15),
    )
    sc = settings.sleep
    return SeedCard(
        age=rng.randint(18, 72),
        birthplace=rng.choice(BIRTHPLACES),
        family_size=rng.randint(1, 7),
        wealth_bracket=_weighted(rng, WEALTH),  # type: ignore[arg-type]
        big_five=big_five,
        dark_traits=dark_traits,
        moral_alignment=alignment,  # type: ignore[arg-type]
        formative_events=rng.sample(FORMATIVE_EVENTS, k=rng.randint(1, 2)),
        bedtime_hour=(sc.default_bedtime + rng.randint(-sc.bedtime_jitter, sc.bedtime_jitter)) % 24,
    )


def roll_founders(rng: random.Random, settings: Settings, n: int) -> list[SeedCard]:
    """Seed cards for the founding population, with at least `birth.min_dark` dark ones."""
    cards = [roll_seed_card(rng, settings) for _ in range(n)]
    missing = min(n, settings.birth.min_dark) - sum(c.moral_alignment in DARK_ALIGNMENTS for c in cards)
    for i in range(n - 1, -1, -1):
        if missing <= 0:
            break
        if cards[i].moral_alignment not in DARK_ALIGNMENTS:
            cards[i] = roll_seed_card(rng, settings, force_dark=True)
            missing -= 1
    return cards


def _level(v: float) -> str:
    if v >= 0.8:
        return "very high"
    if v >= 0.65:
        return "high"
    if v > 0.35:
        return "average"
    if v > 0.2:
        return "low"
    return "very low"


def describe_seed(seed: SeedCard) -> str:
    """The seed card in plain words, so the agent writes a self consistent with its numbers."""
    b, d = seed.big_five, seed.dark_traits
    return "\n".join([
        f"- Age {seed.age}, born in {seed.birthplace}, family of {seed.family_size}, "
        f"{seed.wealth_bracket} background.",
        f"- Openness {_level(b.openness)}, conscientiousness {_level(b.conscientiousness)}, "
        f"extraversion {_level(b.extraversion)}, agreeableness {_level(b.agreeableness)}, "
        f"neuroticism {_level(b.neuroticism)}.",
        f"- Machiavellianism {_level(d.machiavellianism)}, narcissism {_level(d.narcissism)}, "
        f"psychopathy {_level(d.psychopathy)}.",
        f"- Moral baseline: {seed.moral_alignment}.",
        "- Formative events: " + "; ".join(seed.formative_events) + ".",
        f"- Natural bedtime around {seed.bedtime_hour:02d}:00.",
    ])


def _unique_identity(session: Session, rng: random.Random) -> tuple[str, str]:
    """Locally-administered unicast MAC (02:..) and a 10.42.x.y address, both unique."""
    used_mac = set(session.scalars(select(Agent.virtual_mac)))
    used_ip = set(session.scalars(select(Agent.virtual_ip)))
    while True:
        mac = "02:" + ":".join(f"{rng.randrange(256):02x}" for _ in range(5))
        ip = f"10.42.{rng.randrange(256)}.{rng.randrange(1, 255)}"
        if mac not in used_mac and ip not in used_ip:
            return mac, ip


def compose_personality(seed: SeedCard, portrait: SelfPortrait) -> Personality:
    return Personality(
        big_five=seed.big_five, dark_traits=seed.dark_traits, moral_alignment=seed.moral_alignment,
        values=portrait.values, fears=portrait.fears, ambitions=portrait.ambitions,
        secrets=portrait.secrets, speech_style=portrait.speech_style, quirks=portrait.quirks,
        appearance=portrait.appearance, backstory=portrait.backstory,
    )


class _Subject:
    def __init__(self, tier: int, lead: bool = False) -> None:
        self.tier = tier
        self.lead = lead  # a main hero is written by the strong model


async def write_self(gateway: LLMGateway, settings: Settings, seed: SeedCard, *, tier: int,
                     seed_index: int, tick: int, retries: int = 1,
                     taken_names: set[str] | None = None, brief: str | None = None,
                     name: str | None = None, lead: bool = False) -> SelfPortrait:
    """The agent's own model (per its tier) writes its self-portrait from the seed card."""
    subject = _Subject(tier, lead)
    taken = {n.casefold() for n in (taken_names or set())}
    content = ("Your seed card:\n" + describe_seed(seed) + "\n\nRaw values (0-1):\n"
               + seed.model_dump_json(include={"big_five", "dark_traits"}))
    if brief:
        content += f"\n\nWho you are (the world made you so; build your portrait on it):\n{brief}"
    if name:
        content += f"\n\nYour name is {name}. Use exactly this name."
    if taken_names and not name:
        content += "\n\nNames already taken in town (choose a different one): " + ", ".join(
            sorted(taken_names))
    messages = [ChatMessage(role="user", content=content)]
    last_error = ""
    for attempt in range(retries + 1):
        req = LLMRequest(
            purpose="birth", model=model_for(subject, settings.llm.models), tier=subject.tier,
            effort=effort_for(subject, settings.llm.effort), system_rules=BIRTH_RULES,
            messages=messages, max_tokens=settings.birth.max_tokens,
            context={"seed_index": seed_index, "seed": seed.model_dump(), "tick": tick,
                     "attempt": attempt},
        )
        result = await gateway.call(req, tick=tick)
        text = result.text.strip()
        try:
            start, end = text.find("{"), text.rfind("}")
            portrait = SelfPortrait.model_validate(json.loads(text[start:end + 1]))
            if name:
                portrait.name = name
            elif portrait.name.casefold() in taken:
                raise ValueError(f"the name {portrait.name!r} is already taken")
            return portrait
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)[:300]
            messages = messages + [
                ChatMessage(role="assistant", content=text or "(empty)"),
                ChatMessage(role="user", content=f"Invalid ({last_error}). Reply with ONLY the JSON."),
            ]
    raise ValueError(f"birth failed after retries: {last_error}")


async def birth_agent(session: Session, gateway: LLMGateway, settings: Settings,
                      rng: random.Random, *, seed_index: int, home: Location | None,
                      tick: int = 0, seed: SeedCard | None = None, brief: str | None = None,
                      name: str | None = None, lead: bool = False) -> Agent:
    seed = seed or roll_seed_card(rng, settings)
    lo, hi = WEALTH_CENTS[seed.wealth_bracket]
    blo, bhi = WEALTH_BREAD[seed.wealth_bracket]
    money = rng.randint(lo, hi)
    bread = rng.randint(blo, bhi)
    tier = tier_for(seed.starting_role, money, settings.tiers)
    taken = set(session.scalars(select(Agent.name)))
    portrait = await write_self(gateway, settings, seed, tier=tier, seed_index=seed_index,
                                tick=tick, retries=settings.llm.json_retries, taken_names=taken,
                                brief=brief, name=name, lead=lead)
    personality = compose_personality(seed, portrait)
    mac, ip = _unique_identity(session, rng)
    agent = Agent(
        name=portrait.name, tier=tier,
        role=seed.starting_role, seed_card=seed.model_dump(), personality=personality.model_dump(),
        virtual_mac=mac, virtual_ip=ip, home_id=home.id if home else None,
        location_id=home.id if home else None, money_cents=money,
        inventory={"bread": bread} if bread else {},
        hunger=float(rng.randint(10, 35)), energy=float(rng.randint(85, 100)),
        social=float(rng.randint(40, 70)), health=100.0, mood=60.0,
        bedtime_hour=seed.bedtime_hour, born_tick=tick,
    )
    session.add(agent)
    session.flush()
    return agent
