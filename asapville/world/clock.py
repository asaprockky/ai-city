"""Simulated time. 1 tick = 1 simulated hour by default (configurable)."""

from __future__ import annotations

from dataclasses import dataclass

from asapville.config import ClockCfg

SEASONS = ("spring", "summer", "autumn", "winter")


@dataclass(frozen=True)
class WorldTime:
    tick: int
    day: int  # 1-based
    hour: int  # 0..ticks_per_day-1
    season: str
    day_of_season: int  # 1-based
    year: int  # 1-based

    def label(self) -> str:
        return (f"Year {self.year}, {self.season} day {self.day_of_season} (day {self.day}), "
                f"{self.hour:02d}:00")

    @property
    def part_of_day(self) -> str:
        h = self.hour
        if 5 <= h < 12:
            return "morning"
        if 12 <= h < 17:
            return "afternoon"
        if 17 <= h < 21:
            return "evening"
        return "night"


class Clock:
    def __init__(self, cfg: ClockCfg) -> None:
        self.cfg = cfg

    def at(self, tick: int) -> WorldTime:
        tpd = self.cfg.ticks_per_day
        absolute_hours = tick + self.cfg.start_hour
        day_index = absolute_hours // tpd  # 0-based
        hour = absolute_hours % tpd
        season_index = (day_index // self.cfg.days_per_season) % len(SEASONS)
        year = day_index // (self.cfg.days_per_season * len(SEASONS)) + 1
        return WorldTime(
            tick=tick,
            day=day_index + 1,
            hour=hour,
            season=SEASONS[season_index],
            day_of_season=day_index % self.cfg.days_per_season + 1,
            year=year,
        )

    def is_new_day(self, tick: int) -> bool:
        """True on the first tick of a day (hour 0)."""
        return self.at(tick).hour == 0 and tick > 0


def in_sleep_window(hour: int, bedtime: int, sleep_hours: int, ticks_per_day: int = 24) -> bool:
    """Whether `hour` falls inside [bedtime, bedtime + sleep_hours) wrapping midnight."""
    offset = (hour - bedtime) % ticks_per_day
    return offset < sleep_hours
