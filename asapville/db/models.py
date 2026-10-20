"""SQLAlchemy 2.x ORM models.

Portable across SQL Server (mssql+pyodbc) and SQLite:
- human text uses Unicode/UnicodeText (NVARCHAR on SQL Server);
- indexed strings always have a length;
- JSON columns use the generic JSON type (NVARCHAR(max) on SQL Server);
- money is integer cents (100 cents = 1 asap) so arithmetic is exact everywhere.

The schema itself is owned by Alembic (db/migrations). These models must stay in
sync with the latest migration; tests/test_db.py checks that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Unicode,
    UnicodeText,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class WorldState(Base):
    """Singleton row (id=1) holding the global simulation state."""

    __tablename__ = "world_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tick: Mapped[int] = mapped_column(Integer, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    # normal | slow | budget_paused
    mode: Mapped[str] = mapped_column(String(32), default="normal")
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    weather: Mapped[str] = mapped_column(String(32), default="clear")
    temperature_c: Mapped[float] = mapped_column(Float, default=15.0)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    treasury_cents: Mapped[int] = mapped_column(Integer, default=0)  # the town fund (AsapBank)
    wages: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)  # job -> cents/h today
    prices: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)  # item -> cents, set by vote
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Unicode(128))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    x: Mapped[int] = mapped_column(Integer)
    y: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    # Owner / tenant are agents; plain ints (no FK) to avoid an agents<->locations cycle.
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    condition: Mapped[float] = mapped_column(Float, default=100.0)  # 0 = ruined


class Stock(Base):
    """Goods on a shelf or in a store room at a location."""

    __tablename__ = "stocks"
    __table_args__ = (UniqueConstraint("location_id", "item", name="uq_stocks_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), index=True)
    item: Mapped[str] = mapped_column(String(32))
    qty: Mapped[int] = mapped_column(Integer, default=0)


class Stall(Base):
    """A citizen's own business: one good, the owner's price, the owner's till."""

    __tablename__ = "stalls"
    __table_args__ = (UniqueConstraint("owner_id", "location_id", "item", name="uq_stalls_triple"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"), nullable=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), index=True)
    name: Mapped[str] = mapped_column(Unicode(128))
    item: Mapped[str] = mapped_column(String(32))
    price_cents: Mapped[int] = mapped_column(Integer, default=100)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    sold: Mapped[int] = mapped_column(Integer, default=0)
    opened_tick: Mapped[int] = mapped_column(Integer, default=0)


class Proposal(Base):
    """A citizen's proposal at the Town Hall: a law, a wage or a price, decided by vote."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    tick: Mapped[int] = mapped_column(Integer)
    closes_tick: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(UnicodeText)
    kind: Mapped[str] = mapped_column(String(16), default="law")  # law | wage | price
    target: Mapped[str | None] = mapped_column(String(32), nullable=True)  # job or item
    value_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | passed | failed
    yes: Mapped[int] = mapped_column(Integer, default=0)
    no: Mapped[int] = mapped_column(Integer, default=0)
    decided_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("proposal_id", "agent_id", name="uq_votes_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    yes: Mapped[bool] = mapped_column(Boolean)
    tick: Mapped[int] = mapped_column(Integer)


class Election(Base):
    """A town council election: candidates stand, everyone may cast one ballot, the top seats win."""
    __tablename__ = "elections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opens_tick: Mapped[int] = mapped_column(Integer)
    closes_tick: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | decided
    winners: Mapped[list[int]] = mapped_column(JSON, default=list)


class Candidacy(Base):
    __tablename__ = "candidacies"
    __table_args__ = (UniqueConstraint("election_id", "agent_id", name="uq_candidacy"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("elections.id"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    pitch: Mapped[str] = mapped_column(UnicodeText)
    tick: Mapped[int] = mapped_column(Integer)


class Ballot(Base):
    __tablename__ = "ballots"
    __table_args__ = (UniqueConstraint("election_id", "voter_id", name="uq_ballot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("elections.id"), index=True)
    voter_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    candidate_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    tick: Mapped[int] = mapped_column(Integer)


class BankAccount(Base):
    """A citizen's account at AsapBank: savings that earn interest, and at most one loan."""
    __tablename__ = "bank_accounts"

    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    deposit_cents: Mapped[int] = mapped_column(Integer, default=0)
    loan_cents: Mapped[int] = mapped_column(Integer, default=0)  # owed now, interest included
    loan_due_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    defaulted: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_tick: Mapped[int] = mapped_column(Integer, default=0)


class WorldTick(Base):
    """Per-tick snapshot of the town's shelves and fund (dashboard timeline)."""

    __tablename__ = "world_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tick: Mapped[int] = mapped_column(Integer, unique=True)
    treasury_cents: Mapped[int] = mapped_column(Integer, default=0)
    stocks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # location key -> {item: qty}
    weather: Mapped[str] = mapped_column(String(32), default="clear")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Unicode(128))
    tier: Mapped[int] = mapped_column(Integer, default=3)  # 1 notable, 2 manager, 3 worker
    role: Mapped[str] = mapped_column(String(64), default="unemployed")
    seed_card: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    personality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # immutable after birth
    virtual_mac: Mapped[str] = mapped_column(String(17), unique=True)
    virtual_ip: Mapped[str] = mapped_column(String(15), unique=True)

    employer_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    home_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    # Travel: while travel_ticks_left > 0 the agent is on the road to destination_id.
    destination_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    travel_ticks_left: Mapped[int] = mapped_column(Integer, default=0)

    money_cents: Mapped[int] = mapped_column(Integer, default=0)
    inventory: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    diet: Mapped[list[str]] = mapped_column(JSON, default=list)  # the last few things eaten

    health: Mapped[float] = mapped_column(Float, default=100.0)
    hunger: Mapped[float] = mapped_column(Float, default=20.0)  # 100 = starving
    energy: Mapped[float] = mapped_column(Float, default=80.0)
    social: Mapped[float] = mapped_column(Float, default=60.0)
    mood: Mapped[float] = mapped_column(Float, default=60.0)
    mood_label: Mapped[str] = mapped_column(Unicode(64), default="calm")
    reputation: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)  # community -> score

    bedtime_hour: Mapped[int] = mapped_column(Integer, default=22)
    asleep: Mapped[bool] = mapped_column(Boolean, default=False)
    alive: Mapped[bool] = mapped_column(Boolean, default=True)
    born_tick: Mapped[int] = mapped_column(Integer, default=0)
    died_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cause_of_death: Mapped[str | None] = mapped_column(Unicode(256), nullable=True)

    last_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_action_args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_thought: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tick: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True, index=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    summary: Mapped[str] = mapped_column(UnicodeText)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # public | local | private | system   (system = simulation layer, never perceived)
    visibility: Mapped[str] = mapped_column(String(16), default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentTick(Base):
    """Per-tick snapshot of every agent, written at the end of each tick.

    The dashboard replays the town from these rows (where everyone was, how they
    felt, what they were thinking). Citizens never read them.
    """

    __tablename__ = "agent_ticks"
    __table_args__ = (UniqueConstraint("tick", "agent_id", name="uq_agent_ticks_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tick: Mapped[int] = mapped_column(Integer, index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    destination_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    travel_ticks_left: Mapped[int] = mapped_column(Integer, default=0)
    asleep: Mapped[bool] = mapped_column(Boolean, default=False)
    alive: Mapped[bool] = mapped_column(Boolean, default=True)
    hunger: Mapped[float] = mapped_column(Float, default=0.0)
    energy: Mapped[float] = mapped_column(Float, default=0.0)
    social: Mapped[float] = mapped_column(Float, default=0.0)
    health: Mapped[float] = mapped_column(Float, default=0.0)
    mood: Mapped[float] = mapped_column(Float, default=0.0)
    mood_label: Mapped[str] = mapped_column(Unicode(64), default="calm")
    money_cents: Mapped[int] = mapped_column(Integer, default=0)
    inventory: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thought: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    created_at_tick: Mapped[int] = mapped_column(Integer, index=True)
    # observation | action | dialogue | outcome | reflection | plan
    kind: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(UnicodeText)
    importance: Mapped[int] = mapped_column(Integer, default=3)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    last_accessed_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Relationship(Base):
    """Directed edge: how `agent_id` sees `other_id`."""

    __tablename__ = "relationships"
    __table_args__ = (UniqueConstraint("agent_id", "other_id", name="uq_relationships_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    other_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    knows_name: Mapped[bool] = mapped_column(Boolean, default=False)
    affinity: Mapped[float] = mapped_column(Float, default=0.0)  # -1..1
    trust: Mapped[float] = mapped_column(Float, default=0.0)  # -1..1
    debt_cents: Mapped[int] = mapped_column(Integer, default=0)  # >0: other owes agent
    last_interaction_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opinion: Mapped[str] = mapped_column(UnicodeText, default="")
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # what they did to me


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    real_day: Mapped[str] = mapped_column(String(10), index=True)  # UTC YYYY-MM-DD, budget key
    tick: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True, index=True)
    angel_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    purpose: Mapped[str] = mapped_column(String(32))
    backend: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    tier: Mapped[int] = mapped_column(Integer)
    effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    reason: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)  # required for Opus


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tick: Mapped[int] = mapped_column(Integer, index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[int] = mapped_column(Integer, default=1)  # 1..5
    detail: Mapped[str] = mapped_column(UnicodeText)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="engine")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | fixed | ignored
    fix_attempts: Mapped[int] = mapped_column(Integer, default=0)


class Intervention(Base):
    __tablename__ = "interventions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tick: Mapped[int] = mapped_column(Integer)
    anomaly_id: Mapped[int | None] = mapped_column(ForeignKey("anomalies.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))  # state_patch | prompt_patch
    target: Mapped[str] = mapped_column(String(128))
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(UnicodeText, default="")
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class Brand(Base):
    """Registry of every (fictional) brand in the world."""

    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Unicode(128), unique=True)
    category: Mapped[str] = mapped_column(String(48))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    founded_tick: Mapped[int] = mapped_column(Integer, default=0)
    logo_description: Mapped[str] = mapped_column(UnicodeText, default="")
    reputation: Mapped[float] = mapped_column(Float, default=0.0)
