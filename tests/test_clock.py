from asapville.config import ClockCfg
from asapville.world.clock import Clock, in_sleep_window


def test_tick_zero_is_day_one_morning():
    t = Clock(ClockCfg(start_hour=6)).at(0)
    assert (t.day, t.hour, t.season, t.year) == (1, 6, "spring", 1)
    assert t.part_of_day == "morning"


def test_day_and_season_rollover():
    c = Clock(ClockCfg(ticks_per_day=24, start_hour=6, days_per_season=28))
    assert c.at(18).day == 2 and c.at(18).hour == 0
    assert c.is_new_day(18) and not c.is_new_day(19)
    summer = c.at(28 * 24 - 6)
    assert summer.season == "summer" and summer.day_of_season == 1
    assert c.at(4 * 28 * 24).year == 2


def test_sleep_window_wraps_midnight():
    assert in_sleep_window(23, 22, 7)
    assert in_sleep_window(4, 22, 7)
    assert not in_sleep_window(5, 22, 7)
    assert not in_sleep_window(21, 22, 7)
