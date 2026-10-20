"""Pydantic models for everything an agent writes or reads."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MoralAlignment = Literal["lawful-good", "neutral", "selfish", "cruel", "criminal"]


class BigFive(BaseModel):
    openness: float = Field(ge=0, le=1)
    conscientiousness: float = Field(ge=0, le=1)
    extraversion: float = Field(ge=0, le=1)
    agreeableness: float = Field(ge=0, le=1)
    neuroticism: float = Field(ge=0, le=1)


class DarkTraits(BaseModel):
    machiavellianism: float = Field(ge=0, le=1)
    narcissism: float = Field(ge=0, le=1)
    psychopathy: float = Field(ge=0, le=1)


class Appearance(BaseModel):
    hair: str
    build: str
    clothing: str
    signature_item: str
    self_description: str


class Personality(BaseModel):
    """Immutable after birth. Written by the agent itself from its seed card."""

    big_five: BigFive
    dark_traits: DarkTraits
    values: list[str]
    fears: list[str]
    ambitions: list[str]
    secrets: list[str]
    moral_alignment: MoralAlignment
    speech_style: str
    quirks: list[str]
    appearance: Appearance
    backstory: str


class SelfPortrait(BaseModel):
    """What the birth call returns: a name plus the personality fields the agent writes."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=2, max_length=60)
    values: list[str] = Field(min_length=1)
    fears: list[str] = Field(min_length=1)
    ambitions: list[str] = Field(min_length=1)
    secrets: list[str] = Field(default_factory=list)
    speech_style: str
    quirks: list[str] = Field(default_factory=list)
    appearance: Appearance
    backstory: str


class SeedCard(BaseModel):
    """Rolled by the world before birth. The agent can't change these facts."""

    age: int
    birthplace: str
    family_size: int
    wealth_bracket: Literal["poor", "modest", "comfortable", "wealthy"]
    big_five: BigFive
    dark_traits: DarkTraits
    moral_alignment: MoralAlignment
    formative_events: list[str]
    bedtime_hour: int
    starting_role: str = "unemployed"


class ActionName(StrEnum):
    """Closed action vocabulary. The LLM picks one; it never free-texts an action."""

    MOVE = "move"
    WORK = "work"
    BUY = "buy"
    SELL = "sell"
    TALK = "talk"
    GIVE = "give"
    HIRE = "hire"
    FIRE = "fire"
    APPLY = "apply"
    QUIT = "quit"
    BUILD = "build"
    REPAIR = "repair"
    STEAL = "steal"
    ATTACK = "attack"
    REST = "rest"
    SLEEP = "sleep"
    EAT = "eat"
    FORAGE = "forage"
    WRITE_NOTICE = "write_notice"
    WRITE_LETTER = "write_letter"
    PROPOSE_LAW = "propose_law"
    VOTE = "vote"
    FOUND_BUSINESS = "found_business"
    BORROW = "borrow"
    IMPROVISE = "improvise"
    OPEN_STALL = "open_stall"
    BUY_PROPERTY = "buy_property"
    STOCK_STALL = "stock_stall"
    SET_PRICE = "set_price"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    REPAY = "repay"
    COOK = "cook"
    IDLE = "idle"


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thought: str = Field(max_length=2000)
    action: ActionName
    action_args: dict[str, Any] = Field(default_factory=dict)
    dialogue: str | None = Field(default=None, max_length=1000)
    mood: str = Field(default="calm", max_length=64)

    @field_validator("action", mode="before")
    @classmethod
    def _normalise_action(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("action_args", mode="before")
    @classmethod
    def _none_args(cls, v: Any) -> Any:
        return {} if v is None else v


class SeenAgent(BaseModel):
    ref: int  # handle the agent may use in action_args (it's the agent id)
    name: str | None  # None until introduced
    looks: str
    doing: str | None = None
    asleep: bool = False


class Destination(BaseModel):
    key: str
    name: str
    kind: str
    travel_hours: int
    sells: list[str] = Field(default_factory=list)
    jobs: list[str] = Field(default_factory=list)
    note: str | None = None  # e.g. "for sale, 300 asaps" or "owned by Hester Vance"


class ShopItem(BaseModel):
    item: str
    price_cents: int
    qty: int | None = None  # None = never runs out
    seller: str | None = None  # a citizen's stall: its name and who runs it; None = the town shop


class JobHere(BaseModel):
    job: str
    wage_cents: int
    workers: int
    active: int | None = None  # of those, how many turned up in the last day
    no_demand: list[str] = Field(default_factory=list)  # its goods are piled up unsold: no paid work now


class Notice(BaseModel):
    text: str
    author: str  # name if known, else a description or "the town council"
    hours_ago: int


class Perception(BaseModel):
    """Everything an agent knows this tick. Built from its surroundings, never the DB."""

    time_label: str
    part_of_day: str
    season: str
    weather: str
    location_key: str
    location_name: str
    location_kind: str
    location_note: str | None = None
    is_home: bool
    here: list[SeenAgent]
    destinations: list[Destination]
    body: list[str]
    hunger: int
    energy: int
    health: int
    social: int
    money_cents: int
    inventory: dict[str, int]
    notices: list[Notice] = Field(default_factory=list)
    job: str | None = None  # the agent's job, if any
    workplace: str | None = None  # name of where that job is done
    wage_cents: int | None = None
    job_output_cents: int | None = None  # shelf value of what one hour of that job makes
    employer: str | None = None  # the citizen who owns the workplace, if any
    job_unsold: list[str] = Field(default_factory=list)  # their job's goods pile up unsold: no pay now
    bank: str | None = None  # their AsapBank account, in words
    bank_offer: str | None = None  # at the bank: what it will do for them
    kitchen: str | None = None  # what they could cook, and where
    routine: str | None = None  # set when their days have been nothing but routine
    shop: list[ShopItem] = Field(default_factory=list)  # for sale where the agent stands
    jobs_here: list[JobHere] = Field(default_factory=list)
    plan: str | None = None  # the citizen's own plan for today, in their words
    businesses: list[str] = Field(default_factory=list)  # the citizen's own stalls, in words
    proposals: list[str] = Field(default_factory=list)  # open votes, in words (incl. your vote)
    decisions: list[str] = Field(default_factory=list)  # what the town decided lately
    people: list[str] = Field(default_factory=list)  # how you stand with the people you know
    recent_dialogue: list[str] = Field(default_factory=list)
    recent_events: list[str] = Field(default_factory=list)
    available_actions: list[str] = Field(default_factory=list)
