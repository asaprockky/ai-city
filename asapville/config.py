"""Typed configuration loaded from config.yaml (+ env overrides for secrets/DB)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
DEFAULT_DB_URL = "sqlite:///asapville.db"


class WorldCfg(BaseModel):
    name: str = "Asapville"
    seed: int = 1337
    population: int = 5


class ClockCfg(BaseModel):
    ticks_per_day: int = 24
    start_hour: int = 6
    days_per_season: int = 28
    tick_seconds: float = 2.0


class Pricing(BaseModel):
    input: float
    output: float
    cache_read: float
    cache_write: float


class ModelNames(BaseModel):
    opus: str = "claude-opus-5-5"
    sonnet: str = "claude-sonnet-5-5"
    haiku: str = "claude-haiku-5-5"


class EffortCfg(BaseModel):
    angel: str | None = "high"
    notable: str | None = "medium"
    manager: str | None = "low"
    worker: str | None = None


class LLMCfg(BaseModel):
    backend: Literal["fake", "anthropic"] = "fake"
    max_concurrency: int = 8
    max_output_tokens: int = 1024
    json_retries: int = 1
    models: ModelNames = ModelNames()
    effort: EffortCfg = EffortCfg()
    pricing: dict[str, Pricing] = Field(default_factory=dict)


class SlowModeCfg(BaseModel):
    tick_multiplier: float = 4.0
    worker_decide_every: int = 3


class BudgetCfg(BaseModel):
    daily_usd: float = 5.0
    per_agent_daily_usd: float = 0.5
    angel_opus_monthly_usd: float = 10.0
    slow_mode_threshold: float = 0.9
    slow_mode: SlowModeCfg = SlowModeCfg()


class TiersCfg(BaseModel):
    notable_roles: list[str] = Field(default_factory=lambda: ["mayor", "sheriff", "banker", "editor"])
    manager_roles: list[str] = Field(default_factory=list)
    notable_wealth_cents: int = 500_000
    manager_wealth_cents: int = 100_000


class NeedsCfg(BaseModel):
    hunger_per_tick_awake: float = 4.0
    hunger_per_tick_asleep: float = 2.0
    energy_per_tick_awake: float = 4.0
    energy_per_tick_rest: float = 3.0
    energy_per_tick_sleep_home: float = 13.0
    energy_per_tick_sleep_rough: float = 7.0
    social_per_tick_awake: float = 2.0
    social_gain_talk: float = 15.0
    starving_threshold: float = 80.0
    starving_damage: float = 1.5
    famished_damage: float = 4.0
    exhaustion_damage: float = 1.0
    health_regen: float = 0.5
    collapse_energy: float = 0.0
    rough_sleep_mood_penalty: float = 10.0


class SleepCfg(BaseModel):
    hours: int = 7
    default_bedtime: int = 22
    bedtime_jitter: int = 2


class FoodItem(BaseModel):
    nutrition: float


class ForageSpot(BaseModel):
    item: str
    chance: float
    max_qty: int
    energy_cost: float


class ConversationCfg(BaseModel):
    max_turns: int = 6


class MemoryCfg(BaseModel):
    default_importance: int = 3


class LocationCfg(BaseModel):
    key: str
    name: str
    kind: str
    x: int
    y: int
    capacity: int


class Point(BaseModel):
    x: int
    y: int


class HomePlotsCfg(BaseModel):
    count: int = 12
    origin: Point = Point(x=4, y=9)
    columns: int = 4
    spacing: int = 1
    capacity: int = 4


class MapCfg(BaseModel):
    width: int = 20
    height: int = 20
    cells_per_tick: int = 4
    locations: list[LocationCfg] = Field(default_factory=list)
    home_plots: HomePlotsCfg = HomePlotsCfg()


class Settings(BaseModel):
    world: WorldCfg = WorldCfg()
    clock: ClockCfg = ClockCfg()
    llm: LLMCfg = LLMCfg()
    budget: BudgetCfg = BudgetCfg()
    tiers: TiersCfg = TiersCfg()
    needs: NeedsCfg = NeedsCfg()
    sleep: SleepCfg = SleepCfg()
    food: dict[str, FoodItem] = Field(default_factory=dict)
    forage: dict[str, ForageSpot] = Field(default_factory=dict)
    conversation: ConversationCfg = ConversationCfg()
    memory: MemoryCfg = MemoryCfg()
    map: MapCfg = MapCfg()
    db_url: str = DEFAULT_DB_URL


def load_settings(path: str | Path | None = None) -> Settings:
    """Load config.yaml; DB_URL and ASAPVILLE_LLM_BACKEND env vars override."""
    cfg_path = Path(path or os.environ.get("ASAPVILLE_CONFIG", DEFAULT_CONFIG_PATH))
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    settings = Settings.model_validate(raw)
    if db_url := os.environ.get("DB_URL"):
        settings.db_url = db_url
    if backend := os.environ.get("ASAPVILLE_LLM_BACKEND"):
        settings.llm.backend = backend  # type: ignore[assignment]
    return settings
