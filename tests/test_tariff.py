"""Tests for the pure tariff maths (no Home Assistant needed).

All rates here are invented for testing; they do not describe any real plan.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.au_energy_tariff.tariff import (
    PRICE_UNIT_DIVISORS,
    TARGET_TEMPLATES,
    TEMPLATES,
    BlockState,
    DemandCharge,
    DemandState,
    Period,
    Tariff,
    evaluate_reading,
    matching_template,
    parse_minutes,
    to_kwh,
    validate_periods,
)

MEL = ZoneInfo("Australia/Melbourne")

# Invented example tariff (cents), GST-inclusive.
PEAK, OFF_PEAK, SOAK = 41.5, 22.0, 9.0  # c/kWh
SUPPLY = 92.0  # c/day
FEED_IN = 3.5  # c/kWh


def local(y, mo, d, h=0, mi=0, s=0) -> datetime:
    """A Melbourne wall-clock time as an aware UTC datetime."""
    return datetime(y, mo, d, h, mi, s, tzinfo=MEL).astimezone(UTC)


def example(**overrides) -> Tariff:
    """A three-rate time-of-use tariff with a midday solar-soak window."""
    kwargs = {
        "periods": (
            Period("Peak", parse_minutes("16:00"), parse_minutes("21:00"), PEAK),
            Period("Off-peak", parse_minutes("21:00"), parse_minutes("11:00"), OFF_PEAK),
            Period("Solar soak", parse_minutes("11:00"), parse_minutes("16:00"), SOAK),
        ),
        "supply_charge": SUPPLY,
        "tz": MEL,
    }
    kwargs.update(overrides)
    return Tariff(**kwargs)


def one_rate(rate: float, **period_kwargs) -> Tariff:
    """A tariff with one all-day period (flat, step, feed-in, controlled load)."""
    tariff_kwargs = {k: period_kwargs.pop(k) for k in list(period_kwargs) if k in {
        "holiday_check", "holidays_as_weekend", "step_reset_days", "step_anchor",
        "rates_include_gst", "discount_percent", "rate_divisor", "standard_time_windows",
    }}
    return Tariff(
        periods=(Period("All day", 0, 0, rate, **period_kwargs),),
        supply_charge=0.0,
        tz=MEL,
        **tariff_kwargs,
    )


# -- windows -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, "Off-peak"),
        (10, 59, "Off-peak"),
        (11, 0, "Solar soak"),
        (15, 59, "Solar soak"),
        (16, 0, "Peak"),
        (20, 59, "Peak"),
        (21, 0, "Off-peak"),
        (23, 59, "Off-peak"),
    ],
)
def test_window_boundaries(hour, minute, expected):
    assert example().period_at(local(2026, 8, 12, hour, minute)).name == expected


def test_seconds_do_not_leak_across_a_boundary():
    assert example().period_at(local(2026, 8, 12, 15, 59, 59)).name == "Solar soak"
    assert example().period_at(local(2026, 8, 12, 16, 0, 0)).name == "Peak"


def test_windows_follow_local_time_across_spring_forward():
    """Clocks go forward 02:00 -> 03:00 on Sun 4 Oct 2026 (UTC+10 -> UTC+11)."""
    tariff = example()
    # 16:00 local is 06:00 UTC before the change and 05:00 UTC after it.
    assert tariff.period_at(datetime(2026, 10, 3, 6, 0, tzinfo=UTC)).name == "Peak"
    assert tariff.period_at(datetime(2026, 10, 3, 5, 59, tzinfo=UTC)).name == "Solar soak"
    assert tariff.period_at(datetime(2026, 10, 4, 5, 0, tzinfo=UTC)).name == "Peak"
    assert tariff.period_at(datetime(2026, 10, 4, 4, 59, tzinfo=UTC)).name == "Solar soak"
    # 21:00 local: 11:00 UTC before, 10:00 UTC after.
    assert tariff.period_at(datetime(2026, 10, 3, 11, 0, tzinfo=UTC)).name == "Off-peak"
    assert tariff.period_at(datetime(2026, 10, 4, 10, 0, tzinfo=UTC)).name == "Off-peak"
    assert tariff.period_at(datetime(2026, 10, 4, 9, 59, tzinfo=UTC)).name == "Peak"


def test_windows_follow_local_time_across_fall_back():
    """Clocks go back 03:00 -> 02:00 on Sun 4 Apr 2027 (UTC+11 -> UTC+10)."""
    tariff = example()
    # 11:00 local: 00:00 UTC before the change, 01:00 UTC after it.
    assert tariff.period_at(datetime(2027, 4, 3, 23, 59, tzinfo=UTC)).name == "Off-peak"
    assert tariff.period_at(datetime(2027, 4, 4, 0, 59, tzinfo=UTC)).name == "Off-peak"
    assert tariff.period_at(datetime(2027, 4, 4, 1, 0, tzinfo=UTC)).name == "Solar soak"


def test_weekday_specific_periods():
    tariff = example(
        periods=(
            Period("Weekday", 0, 0, 30.0, tuple(range(5))),
            Period("Weekend", 0, 0, 10.0, (5, 6)),
        )
    )
    assert tariff.period_at(local(2026, 8, 14, 12)).name == "Weekday"  # Friday
    assert tariff.period_at(local(2026, 8, 15, 12)).name == "Weekend"  # Saturday
    assert validate_periods(tariff.periods) is None


def test_price_in_dollars():
    assert example().price_at(local(2026, 8, 12, 17)) == pytest.approx(PEAK / 100)


def test_gst_exclusive_adds_ten_percent_to_usage_and_supply():
    tariff = example(rates_include_gst=False)
    assert tariff.price_at(local(2026, 8, 12, 17)) == pytest.approx(PEAK / 100 * 1.1)
    assert tariff.daily_supply_cost == pytest.approx(SUPPLY / 100 * 1.1)


def test_dollar_priced_bills():
    tariff = example(
        periods=(Period("Flat", 0, 0, 0.3150),),
        supply_charge=1.10,
        rate_divisor=PRICE_UNIT_DIVISORS["dollars"],
    )
    assert tariff.price_at(local(2026, 8, 12, 12)) == pytest.approx(0.3150)
    assert tariff.daily_supply_cost == pytest.approx(1.10)


def test_discount_applies_to_usage_only_by_default():
    tariff = example(discount_percent=10.0)
    assert tariff.price_at(local(2026, 8, 12, 17)) == pytest.approx(PEAK / 100 * 0.9)
    assert tariff.daily_supply_cost == pytest.approx(SUPPLY / 100)  # not discounted


def test_discount_can_also_apply_to_the_supply_charge():
    tariff = example(discount_percent=10.0, discount_on_supply=True)
    assert tariff.price_at(local(2026, 8, 12, 17)) == pytest.approx(PEAK / 100 * 0.9)
    assert tariff.daily_supply_cost == pytest.approx(SUPPLY / 100 * 0.9)


def test_free_window_is_just_a_zero_rate():
    tariff = example(
        periods=(
            Period("Free midday", parse_minutes("11:00"), parse_minutes("14:00"), 0.0),
            Period("Other", parse_minutes("14:00"), parse_minutes("11:00"), 30.0),
        )
    )
    assert tariff.price_at(local(2026, 8, 12, 12)) == 0.0
    assert tariff.energy_cost(local(2026, 8, 12, 12), local(2026, 8, 12, 13), 5.0) == 0.0
    assert tariff.price_at(local(2026, 8, 12, 15)) == pytest.approx(0.30)


def test_period_round_trips_through_its_stored_form():
    period = Period("Feed-in", 0, 0, 10.0, (0, 1), step_kwh=8.0, rate_after=3.0)
    assert Period.from_dict(period.to_dict()) == period
    plain = Period("Peak", 960, 1260, 41.5)
    assert "step_kwh" not in plain.to_dict()
    assert Period.from_dict(plain.to_dict()) == plain


# -- daylight saving: wall clock vs standard time ---------------------------------


def test_standard_time_windows_do_not_move_with_daylight_saving():
    """Some plans define windows in standard time (no daylight saving shift)."""
    tariff = example(standard_time_windows=True)
    # 10 Oct 2026 is AEDT (UTC+11); standard time is UTC+10, so the 16:00
    # peak start shows up as 17:00 on the wall clock.
    assert tariff.period_at(local(2026, 10, 10, 16, 59)).name == "Solar soak"
    assert tariff.period_at(local(2026, 10, 10, 17, 0)).name == "Peak"
    # In winter (AEST) the wall clock and standard time agree.
    assert tariff.period_at(local(2026, 8, 12, 15, 59)).name == "Solar soak"
    assert tariff.period_at(local(2026, 8, 12, 16, 0)).name == "Peak"


def test_wall_clock_windows_do_move_with_daylight_saving():
    assert example().period_at(local(2026, 10, 10, 16, 0)).name == "Peak"


def test_standard_time_cost_splits_at_the_shifted_boundary():
    cost = example(standard_time_windows=True).energy_cost(
        local(2026, 10, 10, 16, 30), local(2026, 10, 10, 17, 30), 1.0
    )
    assert cost == pytest.approx((SOAK + PEAK) / 100 / 2)


# -- validation and templates --------------------------------------------------


def test_validate_ok():
    assert validate_periods(example().periods) is None


def test_validate_gap():
    assert validate_periods(example().periods[:2]) == "gap"


def test_validate_overlap():
    extra = Period("Extra", parse_minutes("12:00"), parse_minutes("13:00"), 1.0)
    assert validate_periods((*example().periods, extra)) == "overlap"


def test_validate_empty():
    assert validate_periods(()) == "no_periods"


def test_flat_rate_is_one_all_day_period():
    assert validate_periods((Period("Flat", 0, 0, 25.0),)) is None


@pytest.mark.parametrize("key", [k for k in TEMPLATES if k != "custom"])
def test_every_template_covers_the_whole_week(key):
    periods = [
        Period.from_dict({"name": n, "start": s, "end": e, "rate": 10.0})
        for n, s, e in TEMPLATES[key]
    ]
    assert validate_periods(periods) is None


def test_every_offered_template_exists():
    for target, keys in TARGET_TEMPLATES.items():
        for key in keys:
            assert key in TEMPLATES, (target, key)


def test_parse_minutes():
    assert parse_minutes("16:00:00") == 960
    assert parse_minutes("24:00") == 0
    with pytest.raises(ValueError):
        parse_minutes("25:00")


# -- energy cost -------------------------------------------------------------


def test_cost_within_one_period():
    cost = example().energy_cost(local(2026, 8, 12, 17), local(2026, 8, 12, 17, 1), 2.0)
    assert cost == pytest.approx(2.0 * PEAK / 100)


def test_cost_is_apportioned_across_a_boundary():
    cost = example().energy_cost(local(2026, 8, 12, 15, 30), local(2026, 8, 12, 16, 30), 1.0)
    assert cost == pytest.approx((SOAK + PEAK) / 100 / 2)


def test_cost_across_dst_boundary_uses_real_elapsed_time():
    cost = example().energy_cost(local(2026, 10, 4, 1), local(2026, 10, 4, 4), 3.0)
    assert cost == pytest.approx(3.0 * OFF_PEAK / 100)


def test_cost_gap_spans_three_periods():
    cost = example().energy_cost(local(2026, 8, 12, 15), local(2026, 8, 12, 22), 7.0)
    assert cost == pytest.approx((SOAK + 5 * PEAK + OFF_PEAK) / 100)


def test_zero_length_interval_prices_at_end():
    t = local(2026, 8, 12, 17)
    assert example().energy_cost(t, t, 1.0) == pytest.approx(PEAK / 100)


def test_no_energy_no_cost():
    assert example().energy_cost(local(2026, 8, 12), local(2026, 8, 13), 0.0) == 0.0


# -- feed-in: flat, tiered, time-varying, negative ----------------------------------


def test_flat_feed_in():
    tariff = one_rate(FEED_IN)
    cost = tariff.energy_cost(local(2026, 8, 12, 12), local(2026, 8, 12, 13), 87.5)
    assert cost == pytest.approx(87.5 * FEED_IN / 100)


def test_feed_in_is_entered_as_paid_without_gst():
    # A feed-in tariff built like the sensors do (defaults) gets no GST.
    tariff = one_rate(FEED_IN)
    assert tariff.price_at(local(2026, 8, 12, 12)) == pytest.approx(FEED_IN / 100)


def test_tiered_feed_in_pays_more_for_the_first_kwh_each_day():
    tariff = one_rate(10.0, step_kwh=8.0, rate_after=3.0)
    state = BlockState()
    # 6 kWh midday, all inside the 8 kWh allowance.
    first = tariff.energy_cost(local(2026, 8, 12, 11), local(2026, 8, 12, 12), 6.0, state)
    assert first == pytest.approx(6 * 0.10)
    # 5 more: 2 more at 10c, then 3 at 3c.
    second = tariff.energy_cost(local(2026, 8, 12, 12), local(2026, 8, 12, 13), 5.0, state)
    assert second == pytest.approx(2 * 0.10 + 3 * 0.03)
    # The next local day starts a fresh allowance.
    third = tariff.energy_cost(local(2026, 8, 13, 11), local(2026, 8, 13, 12), 4.0, state)
    assert third == pytest.approx(4 * 0.10)


def test_time_varying_feed_in_with_a_zero_and_a_negative_window():
    tariff = Tariff(
        periods=(
            Period("Evening", 960, 1260, 8.0),
            Period("Midday", 660, 960, -2.5),  # a charge to export
            Period("Overnight", 1260, 660, 0.0),
        ),
        supply_charge=0.0,
        tz=MEL,
    )
    assert validate_periods(tariff.periods) is None
    assert tariff.energy_cost(local(2026, 8, 12, 17), local(2026, 8, 12, 18), 2.0) == pytest.approx(0.16)
    assert tariff.energy_cost(local(2026, 8, 12, 12), local(2026, 8, 12, 13), 4.0) == pytest.approx(-0.10)
    assert tariff.energy_cost(local(2026, 8, 12, 23), local(2026, 8, 13, 0, 30), 1.0) == 0.0


# -- allowances: step rates and capped free power -----------------------------------


def test_step_rates_first_kwh_per_day():
    tariff = one_rate(30.0, step_kwh=10.0, rate_after=36.0)
    state = BlockState()
    a = tariff.energy_cost(local(2026, 8, 12, 8), local(2026, 8, 12, 9), 6.0, state)
    b = tariff.energy_cost(local(2026, 8, 12, 9), local(2026, 8, 12, 10), 6.0, state)
    assert a == pytest.approx(6 * 0.30)
    assert b == pytest.approx(4 * 0.30 + 2 * 0.36)
    # Next local day resets.
    c = tariff.energy_cost(local(2026, 8, 13, 8), local(2026, 8, 13, 9), 6.0, state)
    assert c == pytest.approx(6 * 0.30)


def test_capped_free_power_only_counts_usage_in_its_window():
    tariff = example(
        periods=(
            Period("Free midday", 660, 840, 0.0, step_kwh=24.0, rate_after=30.0),
            Period("Other", 840, 660, 30.0),
        )
    )
    state = BlockState()
    # Usage outside the free window does not eat the allowance.
    tariff.energy_cost(local(2026, 8, 12, 8), local(2026, 8, 12, 9), 50.0, state)
    # 30 kWh inside the window: first 24 free, 6 charged.
    cost = tariff.energy_cost(local(2026, 8, 12, 12), local(2026, 8, 12, 13), 30.0, state)
    assert cost == pytest.approx(6 * 0.30)


def test_windows_with_the_same_name_share_one_allowance():
    tariff = example(
        periods=(
            Period("Shoulder", 420, 840, 20.0, step_kwh=10.0, rate_after=40.0),
            Period("Peak", 840, 1200, 50.0),
            Period("Shoulder", 1200, 1320, 20.0, step_kwh=10.0, rate_after=40.0),
            Period("Off-peak", 1320, 420, 10.0),
        )
    )
    assert validate_periods(tariff.periods) is None
    state = BlockState()
    tariff.energy_cost(local(2026, 8, 12, 8), local(2026, 8, 12, 9), 8.0, state)
    # 4 kWh in the evening shoulder window (20:00-22:00): only 2 kWh of the
    # shared 10 kWh allowance is left, so 2 kWh at 20c and 2 kWh at 40c.
    cost = tariff.energy_cost(local(2026, 8, 12, 20, 30), local(2026, 8, 12, 21, 30), 4.0, state)
    assert cost == pytest.approx(2 * 0.20 + 2 * 0.40)
    assert state.used["Shoulder"] == pytest.approx(12.0)


def test_allowance_cycle_longer_than_a_day():
    tariff = one_rate(
        30.0, step_kwh=10.0, rate_after=40.0, step_reset_days=3, step_anchor=date(2026, 8, 10)
    )
    state = BlockState()
    day10 = tariff.energy_cost(local(2026, 8, 10, 8), local(2026, 8, 10, 9), 6.0, state)
    day12 = tariff.energy_cost(local(2026, 8, 12, 8), local(2026, 8, 12, 9), 6.0, state)
    day13 = tariff.energy_cost(local(2026, 8, 13, 8), local(2026, 8, 13, 9), 6.0, state)
    assert day10 == pytest.approx(6 * 0.30)
    assert day12 == pytest.approx(4 * 0.30 + 2 * 0.40)  # same 3-day cycle
    assert day13 == pytest.approx(6 * 0.30)  # fresh cycle


def test_a_gap_spanning_midnight_resets_the_allowance_at_midnight():
    tariff = one_rate(30.0, step_kwh=10.0, rate_after=40.0)
    state = BlockState()
    # 12 kWh evenly from 23:00 to 01:00: 6 kWh each side of local midnight.
    cost = tariff.energy_cost(local(2026, 8, 12, 23), local(2026, 8, 13, 1), 12.0, state)
    assert cost == pytest.approx(12 * 0.30)  # each day stays within its allowance


def test_allowance_state_round_trips():
    state = BlockState(key="2026-08-12", used={"Peak": 3.5})
    assert BlockState.from_dict(state.to_dict()) == state
    assert BlockState.from_dict(None) == BlockState()


# -- public holidays ---------------------------------------------------------------


def _weekday_weekend(**kwargs) -> Tariff:
    return example(
        periods=(
            Period("Weekday", 0, 0, 30.0, tuple(range(5))),
            Period("Weekend", 0, 0, 10.0, (5, 6)),
        ),
        holiday_check=lambda d: d == date(2026, 8, 12),  # a Wednesday
        **kwargs,
    )


def test_public_holiday_counts_as_a_normal_day_by_default():
    assert _weekday_weekend().period_at(local(2026, 8, 12, 12)).name == "Weekday"


def test_public_holiday_can_take_weekend_rates():
    tariff = _weekday_weekend(holidays_as_weekend=True)
    assert tariff.period_at(local(2026, 8, 12, 12)).name == "Weekend"
    assert tariff.period_at(local(2026, 8, 13, 12)).name == "Weekday"  # next day normal


def test_holiday_switches_rates_at_local_midnight():
    tariff = _weekday_weekend(holidays_as_weekend=True)
    # 1 kWh from 23:30 on Tue 11th to 00:30 on the holiday: half weekday, half weekend.
    cost = tariff.energy_cost(local(2026, 8, 11, 23, 30), local(2026, 8, 12, 0, 30), 1.0)
    assert cost == pytest.approx((0.30 + 0.10) / 2)


# -- fixed daily charges -------------------------------------------------------


def test_supply_charge_is_a_fixed_amount_per_day():
    assert example().daily_supply_cost == pytest.approx(SUPPLY / 100)


def test_other_daily_charge_and_credit():
    assert example(extra_daily=10.0).daily_supply_cost == pytest.approx((SUPPLY + 10) / 100)
    assert example(extra_daily=-20.0).daily_supply_cost == pytest.approx((SUPPLY - 20) / 100)


def test_zero_supply_charge():
    assert example(supply_charge=0).daily_supply_cost == 0


# -- demand charge ---------------------------------------------------------------


def demand_tariff(basis: str = "day", rate: float = 20.0, **kwargs) -> Tariff:
    window = Period("Demand window", 960, 1260, 0.0, tuple(range(7)))
    return example(demand=DemandCharge(rate, basis, window), **kwargs)


def test_per_day_demand_charge_tracks_the_monthly_maximum():
    tariff, state = demand_tariff("day", 20.0), DemandState()
    # Day 1 of the month starts: nothing yet to charge.
    assert tariff.demand_daily(state, date(2026, 8, 1)) == 0.0
    # 1.5 kWh in 17:00-17:30 is 3 kW. The interval closes on the next reading.
    tariff.demand_register(state, local(2026, 8, 1, 17), local(2026, 8, 1, 17, 30), 1.5)
    cost = tariff.demand_flush(state, local(2026, 8, 1, 17, 31))
    assert cost == pytest.approx(0.20 * 3.0 * 1)  # 20c/kW/day x 3 kW x 1 day so far
    # Day 2: charged on the maximum so far.
    assert tariff.demand_daily(state, date(2026, 8, 2)) == pytest.approx(0.20 * 3.0)
    # A new 4 kW peak on day 2 back-charges the extra 1 kW for both days.
    tariff.demand_register(state, local(2026, 8, 2, 18), local(2026, 8, 2, 18, 30), 2.0)
    assert tariff.demand_flush(state, local(2026, 8, 2, 18, 31)) == pytest.approx(0.20 * 1.0 * 2)
    # Total so far is rate x max x days = 20c x 4 kW x 2 days.
    assert state.max_kw == pytest.approx(4.0)


def test_per_month_demand_charge_is_charged_on_new_maxima_only():
    tariff, state = demand_tariff("month", 700.0), DemandState()
    assert tariff.demand_daily(state, date(2026, 8, 1)) == 0.0
    tariff.demand_register(state, local(2026, 8, 1, 17), local(2026, 8, 1, 17, 30), 1.5)
    assert tariff.demand_flush(state, local(2026, 8, 1, 18)) == pytest.approx(7.0 * 3.0)
    # A lower peak later changes nothing.
    tariff.demand_register(state, local(2026, 8, 5, 17), local(2026, 8, 5, 17, 30), 1.0)
    assert tariff.demand_flush(state, local(2026, 8, 5, 18)) == 0.0


def test_demand_outside_the_window_is_ignored():
    tariff, state = demand_tariff("month", 700.0), DemandState()
    tariff.demand_daily(state, date(2026, 8, 1))
    tariff.demand_register(state, local(2026, 8, 1, 12), local(2026, 8, 1, 12, 30), 5.0)
    assert tariff.demand_flush(state, local(2026, 8, 1, 13)) == 0.0
    assert state.max_kw == 0.0


def test_demand_uses_thirty_minute_intervals_not_instants():
    tariff, state = demand_tariff("month", 100.0), DemandState()
    tariff.demand_daily(state, date(2026, 8, 1))
    # 3 kWh evenly over 17:00-18:00 is two intervals of 1.5 kWh = 3 kW each.
    tariff.demand_register(state, local(2026, 8, 1, 17), local(2026, 8, 1, 18), 3.0)
    tariff.demand_flush(state, local(2026, 8, 1, 18, 31))
    assert state.max_kw == pytest.approx(3.0)


def test_demand_rolls_over_at_a_new_month():
    tariff, state = demand_tariff("day", 20.0), DemandState()
    tariff.demand_daily(state, date(2026, 8, 31))
    tariff.demand_register(state, local(2026, 8, 31, 17), local(2026, 8, 31, 17, 30), 1.5)
    tariff.demand_flush(state, local(2026, 8, 31, 18))
    assert state.max_kw == pytest.approx(3.0)
    # First day of September: the maximum restarts from zero.
    assert tariff.demand_daily(state, date(2026, 9, 1)) == 0.0
    assert state.month == "2026-09"
    assert state.max_kw == 0.0
    assert state.days == 1


def test_demand_window_can_be_weekdays_only():
    window = Period("Demand window", 960, 1260, 0.0, (0, 1, 2, 3, 4))
    tariff, state = example(demand=DemandCharge(100.0, "month", window)), DemandState()
    tariff.demand_daily(state, date(2026, 8, 15))
    # Saturday 15 Aug 2026: not in the window.
    tariff.demand_register(state, local(2026, 8, 15, 17), local(2026, 8, 15, 17, 30), 2.0)
    assert tariff.demand_flush(state, local(2026, 8, 15, 18)) == 0.0


def test_demand_state_round_trips():
    state = DemandState(month="2026-08", max_kw=3.5, days=4, interval=123, interval_kwh=0.5)
    assert DemandState.from_dict(state.to_dict()) == state
    assert DemandState.from_dict(None) == DemandState()


def test_no_demand_charge_does_nothing():
    tariff, state = example(), DemandState()
    assert tariff.demand_register(state, local(2026, 8, 1, 17), local(2026, 8, 1, 18), 5.0) == 0.0
    assert tariff.demand_daily(state, date(2026, 8, 1)) == 0.0
    assert tariff.demand_flush(state, local(2026, 8, 1, 19)) == 0.0


# -- a bill-shaped sanity check (all numbers invented) ----------------------


def test_bill_style_line_items_add_up():
    """Usage x rate per line, plus 30 days of supply, less feed-in credit."""
    tariff = example()
    peak = 120.0 * PEAK / 100
    off_peak = 210.5 * OFF_PEAK / 100
    soak = 80.25 * SOAK / 100
    supply = 30 * tariff.daily_supply_cost
    feed_in = 350.0 * FEED_IN / 100

    lines = [round(x, 2) for x in (peak, off_peak, soak, supply)] + [-round(feed_in, 2)]
    assert sum(lines) == pytest.approx(
        peak + off_peak + soak + supply - feed_in, abs=0.03
    )


# -- units and deltas --------------------------------------------------------


def test_unit_conversion():
    assert to_kwh(1500, "Wh") == 1.5
    assert to_kwh(2.25, "kWh") == 2.25
    assert to_kwh(2.117874, "MWh") == pytest.approx(2117.874)
    assert to_kwh(1, "W") is None
    assert to_kwh(1, None) is None


def test_first_reading_sets_baseline_only():
    assert evaluate_reading(None, 5000.0, 0, 50) == (0.0, 5000.0, "initial")


def test_normal_delta():
    assert evaluate_reading(5000.0, 5000.5, 60, 50) == (0.5, 5000.5, "ok")


def test_unchanged():
    assert evaluate_reading(5000.0, 5000.0, 60, 50) == (0.0, 5000.0, "unchanged")


def test_reset_rebaselines_without_negative_delta():
    assert evaluate_reading(5000.0, 3.0, 60, 50) == (0.0, 3.0, "reset")


def test_tiny_dip_is_noise_and_recovery_is_not_double_counted():
    delta, baseline, reason = evaluate_reading(5000.0, 4999.95, 60, 50)
    assert (delta, baseline, reason) == (0.0, 5000.0, "noise")
    delta, baseline, reason = evaluate_reading(baseline, 5000.05, 60, 50)
    assert reason == "ok"
    assert delta == pytest.approx(0.05)


def test_implausible_jump_is_skipped():
    assert evaluate_reading(5000.0, 6000.0, 60, 50) == (0.0, 6000.0, "jump")


def test_long_gap_allows_a_large_delta():
    delta, _, reason = evaluate_reading(5000.0, 5100.0, 4 * 3600, 50)
    assert reason == "ok"
    assert delta == 100.0


# -- preselecting the saved structure -----------------------------------------


def _saved(*periods: Period) -> list[dict]:
    return [p.to_dict() for p in periods]


def test_matching_template_finds_the_solar_soak_structure():
    saved = _saved(*example().periods)
    assert matching_template("usage", saved) == "solar_soak"


def test_matching_template_ignores_names_and_prices():
    saved = _saved(
        Period("A", parse_minutes("16:00"), parse_minutes("21:00"), 1.0),
        Period("B", parse_minutes("21:00"), parse_minutes("11:00"), 2.0),
        Period("C", parse_minutes("11:00"), parse_minutes("16:00"), 3.0),
    )
    assert matching_template("usage", saved) == "solar_soak"


def test_matching_template_tells_flat_from_step_by_the_allowance():
    assert matching_template("usage", _saved(Period("x", 0, 0, 30.0))) == "flat"
    stepped = _saved(Period("x", 0, 0, 30.0, step_kwh=10.0, rate_after=36.0))
    assert matching_template("usage", stepped) == "step"


def test_matching_template_feed_in_flat_and_tiered():
    assert matching_template("feed_in", _saved(Period("f", 0, 0, 5.0))) == "feed_flat"
    tiered = _saved(Period("f", 0, 0, 10.0, step_kwh=8.0, rate_after=3.0))
    assert matching_template("feed_in", tiered) == "feed_tiered"


def test_matching_template_controlled_load():
    assert matching_template("controlled_load", _saved(Period("c", 0, 0, 18.0))) == "cl_flat"


def test_matching_template_shoulder_with_two_windows_of_one_name():
    saved = _saved(*[Period(n, parse_minutes(s), parse_minutes(e), 1.0) for n, s, e in TEMPLATES["peak_shoulder_offpeak"]])
    assert matching_template("usage", saved) == "peak_shoulder_offpeak"


def test_matching_template_is_custom_for_day_specific_or_unrecognised_rates():
    weekday = _saved(Period("w", 0, 0, 30.0, (0, 1, 2, 3, 4)), Period("e", 0, 0, 10.0, (5, 6)))
    assert matching_template("usage", weekday) == "custom"
    odd = _saved(Period("a", 0, parse_minutes("12:00"), 1.0), Period("b", parse_minutes("12:00"), 0, 2.0))
    assert matching_template("usage", odd) == "custom"


def test_matching_template_is_none_when_nothing_is_saved():
    assert matching_template("usage", []) is None
