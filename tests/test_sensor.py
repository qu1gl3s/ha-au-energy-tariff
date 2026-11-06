"""Integration tests: sensors, restore and the config flow.

All rates here are invented for testing; they do not describe any real plan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.au_energy_tariff.const import (
    CONF_CL_ENTITY,
    CONF_CL_PERIODS,
    CONF_CL_SUPPLY_CHARGE,
    CONF_CURRENCY,
    CONF_DEMAND_BASIS,
    CONF_DEMAND_DAYS,
    CONF_DEMAND_ENABLED,
    CONF_DEMAND_END,
    CONF_DEMAND_RATE,
    CONF_DEMAND_START,
    CONF_DISCOUNT_PERCENT,
    CONF_DISCOUNT_SCOPE,
    CONF_EXPORT_ENTITY,
    CONF_EXTRA_DAILY,
    CONF_FEED_IN_PERIODS,
    CONF_HOLIDAY_STATE,
    CONF_HOLIDAYS_AS,
    CONF_IMPORT_ENTITY,
    CONF_MAX_POWER_KW,
    CONF_PERIODS,
    CONF_PRICE_UNIT,
    CONF_RATES_INCLUDE_GST,
    CONF_STEP_RESET_DAYS,
    CONF_SUPPLY_CHARGE,
    CONF_WINDOW_TIME,
    DOMAIN,
)

IMPORT = "sensor.grid_import"
EXPORT = "sensor.grid_export"
CL = "sensor.grid_cl"
COST = "sensor.test_import_cost"
CREDIT = "sensor.test_export_credit"
CL_COST = "sensor.test_controlled_load_cost"

PEAK, OFF_PEAK, SOAK = 41.5, 22.0, 9.0  # c/kWh
SUPPLY = 92.0  # c/day
FEED_IN = 3.5  # c/kWh

DAYS = list(range(7))
PERIODS = [
    {"name": "Peak", "start": "16:00", "end": "21:00", "rate": PEAK, "days": DAYS},
    {"name": "Off-peak", "start": "21:00", "end": "11:00", "rate": OFF_PEAK, "days": DAYS},
    {"name": "Solar soak", "start": "11:00", "end": "16:00", "rate": SOAK, "days": DAYS},
]
FLAT_FEED_IN = [{"name": "Feed-in", "start": "00:00", "end": "00:00", "rate": FEED_IN, "days": DAYS}]
FLAT_CL = [{"name": "Controlled load", "start": "00:00", "end": "00:00", "rate": 18.0, "days": DAYS}]


def _utc(y, mo, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def _energy(unit: str = "kWh") -> dict:
    return {"unit_of_measurement": unit, "state_class": "total_increasing", "device_class": "energy"}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    """Allow Home Assistant to load the custom integration."""


async def _setup(
    hass: HomeAssistant,
    *,
    supply: float = 0.0,
    export: bool = False,
    controlled_load: bool = False,
    max_power: float = 1000.0,
    price_unit: str = "cents",
    periods: list | None = None,
    feed_in_periods: list | None = None,
    cl_periods: list | None = None,
    cl_supply: float = 0.0,
    discount: float = 0.0,
    options: dict | None = None,
) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Australia/Melbourne")
    data = {CONF_IMPORT_ENTITY: IMPORT, CONF_CURRENCY: "AUD", CONF_PRICE_UNIT: price_unit}
    if export:
        data[CONF_EXPORT_ENTITY] = EXPORT
    if controlled_load:
        data[CONF_CL_ENTITY] = CL
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data=data,
        options={
            CONF_PERIODS: PERIODS if periods is None else periods,
            CONF_FEED_IN_PERIODS: FLAT_FEED_IN if feed_in_periods is None else feed_in_periods,
            CONF_CL_PERIODS: FLAT_CL if cl_periods is None else cl_periods,
            CONF_SUPPLY_CHARGE: supply,
            CONF_CL_SUPPLY_CHARGE: cl_supply,
            CONF_RATES_INCLUDE_GST: True,
            CONF_DISCOUNT_PERCENT: discount,
            CONF_MAX_POWER_KW: max_power,
            **(options or {}),
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _value(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


async def _first_charge(hass: HomeAssistant, freezer) -> None:
    """Let a brand new sensor add its first day's charges (a few seconds after setup)."""
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _step(hass, entity: str, value: str, unit="kWh"):
    hass.states.async_set(entity, value, _energy(unit))
    await hass.async_block_till_done()


# -- cost accrual --------------------------------------------------------------


async def test_import_cost_uses_price_in_force(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))  # 16:00 AEST = peak
    hass.states.async_set(IMPORT, "1000", _energy())
    await _setup(hass)
    assert _value(hass, COST) == 0.0  # first reading is a baseline only

    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "1001")
    assert _value(hass, COST) == pytest.approx(PEAK / 100)

    # Move to 21:30 local (11:30 UTC) and import 2 kWh since the last reading
    # at 06:01 UTC: 299 minutes were peak, the final 30 off-peak.
    freezer.move_to(_utc(2026, 8, 12, 11, 30))
    await _step(hass, IMPORT, "1003")
    expected = PEAK / 100 + 2 * (299 / 329) * PEAK / 100 + 2 * (30 / 329) * OFF_PEAK / 100
    assert _value(hass, COST) == pytest.approx(expected, abs=1e-5)


async def test_state_is_monetary_total_with_currency(hass: HomeAssistant) -> None:
    hass.states.async_set(IMPORT, "10", _energy())
    await _setup(hass)
    state = hass.states.get(COST)
    assert state.attributes["device_class"] == "monetary"
    assert state.attributes["state_class"] == "total"
    assert state.attributes["unit_of_measurement"] == "AUD"


async def test_mwh_source_is_converted(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))  # peak
    hass.states.async_set(IMPORT, "2.000000", _energy("MWh"))
    await _setup(hass)
    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "2.001000", "MWh")  # +1 kWh
    assert _value(hass, COST) == pytest.approx(PEAK / 100)


async def test_dollar_priced_plan_with_discount(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(
        hass,
        price_unit="dollars",
        periods=[{"name": "Flat", "start": "00:00", "end": "00:00", "rate": 0.30, "days": DAYS}],
        discount=10.0,
    )
    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "101")
    assert _value(hass, COST) == pytest.approx(0.30 * 0.9)


async def test_unavailable_and_unknown_are_ignored(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(hass)
    for bad in ("unavailable", "unknown", "garbage", "nan"):
        freezer.tick(timedelta(seconds=30))
        await _step(hass, IMPORT, bad)
        assert _value(hass, COST) == 0.0
    freezer.tick(timedelta(seconds=30))
    await _step(hass, IMPORT, "101")
    assert _value(hass, COST) == pytest.approx(PEAK / 100)


async def test_reset_never_goes_negative_and_resumes(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    hass.states.async_set(IMPORT, "5000", _energy())
    await _setup(hass)
    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "5001")
    before = _value(hass, COST)

    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "2")  # meter replaced / counter reset
    assert _value(hass, COST) == before

    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "3")
    assert _value(hass, COST) == pytest.approx(before + PEAK / 100)


async def test_implausible_jump_is_not_billed(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    hass.states.async_set(IMPORT, "5000", _energy())
    await _setup(hass, max_power=50.0)
    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "9000")  # +4000 kWh in a minute
    assert _value(hass, COST) == 0.0
    freezer.tick(timedelta(hours=1))
    await _step(hass, IMPORT, "9001")  # normal again from the new baseline
    assert _value(hass, COST) == pytest.approx(PEAK / 100)


# -- supply charge: a fixed amount per local day --------------------------------


async def test_supply_charge_is_a_flat_daily_step(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 11, 14, 0))  # local midnight, 12 Aug
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(hass, supply=SUPPLY)
    assert _value(hass, COST) == 0.0  # the starting value is published first
    await _first_charge(hass, freezer)
    assert _value(hass, COST) == pytest.approx(SUPPLY / 100)

    freezer.move_to(_utc(2026, 8, 11, 20, 0))
    async_fire_time_changed(hass, _utc(2026, 8, 11, 20, 0))
    await hass.async_block_till_done()
    assert _value(hass, COST) == pytest.approx(SUPPLY / 100)

    freezer.move_to(_utc(2026, 8, 12, 14, 0))
    async_fire_time_changed(hass, _utc(2026, 8, 12, 14, 0))
    await hass.async_block_till_done()
    assert _value(hass, COST) == pytest.approx(2 * SUPPLY / 100)
    assert hass.states.get(COST).attributes["supply_charge"] == pytest.approx(2 * SUPPLY / 100)


async def test_other_daily_credit_reduces_the_daily_total(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 11, 14, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(hass, supply=SUPPLY, options={CONF_EXTRA_DAILY: -20.0})
    await _first_charge(hass, freezer)
    assert _value(hass, COST) == pytest.approx((SUPPLY - 20.0) / 100)


async def test_first_run_publishes_the_starting_value_before_the_first_charge(
    hass: HomeAssistant, freezer
) -> None:
    """Long-term statistics start from a sensor's first recorded value.

    If the first day's supply charge were already in that first value it would
    be missing from the statistics, so a new sensor publishes 0 first.
    """
    freezer.move_to(_utc(2026, 8, 11, 14, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    seen: list[float] = []

    def _record(event) -> None:
        if event.data["entity_id"] == COST and (new := event.data["new_state"]) is not None:
            if new.state not in ("unknown", "unavailable"):
                seen.append(float(new.state))

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    await _setup(hass, supply=SUPPLY)
    assert seen and seen[0] == 0.0
    assert all(value == 0.0 for value in seen)  # nothing charged yet

    await _first_charge(hass, freezer)
    assert seen[-1] == pytest.approx(SUPPLY / 100)


async def test_restart_of_an_existing_sensor_still_catches_up_immediately(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    extra = {
        "native_value": 1.0,
        "native_unit_of_measurement": "AUD",
        "energy_total": 0.0,
        "supply_total": 1.0,
        "baseline_kwh": 100.0,
        "energy_ts": _utc(2026, 8, 12, 5, 59).isoformat(),
        "supply_date": "2026-08-11",  # yesterday was the last day charged
    }
    mock_restore_cache_with_extra_data(hass, ((State(COST, "1.0"), extra),))
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(hass, supply=SUPPLY)
    # No waiting for a delayed charge: today is added as soon as it starts.
    assert _value(hass, COST) == pytest.approx(1.0 + SUPPLY / 100)


async def test_supply_charge_is_one_per_day_across_a_dst_change(
    hass: HomeAssistant, freezer
) -> None:
    """The 4 Oct 2026 local day is only 23 hours long but still one charge."""
    freezer.move_to(_utc(2026, 10, 3, 14, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(hass, supply=SUPPLY)
    await _first_charge(hass, freezer)
    assert _value(hass, COST) == pytest.approx(SUPPLY / 100)

    freezer.move_to(_utc(2026, 10, 4, 13, 0))  # next local midnight (AEDT, +11)
    async_fire_time_changed(hass, _utc(2026, 10, 4, 13, 0))
    await hass.async_block_till_done()
    assert _value(hass, COST) == pytest.approx(2 * SUPPLY / 100)


# -- feed-in ------------------------------------------------------------------------


async def test_export_credit(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 2, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    hass.states.async_set(EXPORT, "50", _energy())
    await _setup(hass, export=True)
    freezer.tick(timedelta(hours=1))
    await _step(hass, EXPORT, "150")  # 100 kWh exported
    assert _value(hass, CREDIT) == pytest.approx(100 * FEED_IN / 100)


async def test_tiered_feed_in_pays_more_for_the_first_kwh_each_day(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(_utc(2026, 8, 12, 0, 0))  # 10:00 local
    hass.states.async_set(IMPORT, "100", _energy())
    hass.states.async_set(EXPORT, "50", _energy())
    tiered = [
        {"name": "Feed-in", "start": "00:00", "end": "00:00", "rate": 10.0, "days": DAYS,
         "step_kwh": 8.0, "rate_after": 3.0}
    ]
    await _setup(hass, export=True, feed_in_periods=tiered)
    freezer.tick(timedelta(hours=1))
    await _step(hass, EXPORT, "62")  # 12 kWh: 8 at 10c, 4 at 3c
    assert _value(hass, CREDIT) == pytest.approx(8 * 0.10 + 4 * 0.03)
    freezer.tick(timedelta(hours=1))
    await _step(hass, EXPORT, "64")  # 2 more, all past the allowance
    assert _value(hass, CREDIT) == pytest.approx(8 * 0.10 + 6 * 0.03)


async def test_negative_feed_in_reduces_the_credit(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 2, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    hass.states.async_set(EXPORT, "50", _energy())
    negative = [{"name": "Feed-in", "start": "00:00", "end": "00:00", "rate": -2.0, "days": DAYS}]
    await _setup(hass, export=True, feed_in_periods=negative)
    freezer.tick(timedelta(hours=1))
    await _step(hass, EXPORT, "60")
    assert _value(hass, CREDIT) == pytest.approx(-0.20)


# -- controlled load -----------------------------------------------------------


async def test_controlled_load_has_its_own_rate_and_supply(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 14, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    hass.states.async_set(CL, "500", _energy())
    await _setup(hass, controlled_load=True, supply=SUPPLY, cl_supply=1.1)
    await _first_charge(hass, freezer)
    # Daily supply: main supply on the import sensor, controlled-load supply on its own.
    assert _value(hass, COST) == pytest.approx(SUPPLY / 100)
    assert _value(hass, CL_COST) == pytest.approx(1.1 / 100)

    freezer.tick(timedelta(hours=1))
    await _step(hass, CL, "510")  # 10 kWh at 18c
    assert _value(hass, CL_COST) == pytest.approx(1.1 / 100 + 10 * 0.18)
    assert _value(hass, COST) == pytest.approx(SUPPLY / 100)  # untouched


# -- demand charge ----------------------------------------------------------------


async def test_demand_charge_accrues_from_the_peak_half_hour(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))  # 16:00 local: start of the window
    hass.states.async_set(IMPORT, "1000", _energy())
    await _setup(
        hass,
        options={
            CONF_DEMAND_ENABLED: True,
            CONF_DEMAND_RATE: 20.0,  # 20c per kW per day
            CONF_DEMAND_BASIS: "day",
            CONF_DEMAND_START: "16:00",
            CONF_DEMAND_END: "21:00",
            CONF_DEMAND_DAYS: DAYS,
        },
    )
    # 1.5 kWh in the first half hour is 3 kW.
    freezer.move_to(_utc(2026, 8, 12, 6, 30))
    await _step(hass, IMPORT, "1001.5")
    # The interval closes on the next half-hour tick.
    freezer.move_to(_utc(2026, 8, 12, 6, 30, 10))
    async_fire_time_changed(hass, _utc(2026, 8, 12, 6, 30, 10))
    await hass.async_block_till_done()

    attrs = hass.states.get(COST).attributes
    assert attrs["peak_demand_kw"] == pytest.approx(3.0)
    assert attrs["demand_charge"] == pytest.approx(0.20 * 3.0)  # 1 day so far
    assert _value(hass, COST) == pytest.approx(1.5 * PEAK / 100 + 0.20 * 3.0)


# -- public holidays ---------------------------------------------------------------


async def test_demand_switched_on_after_the_sensor_started_charges_today(
    hass: HomeAssistant, freezer
) -> None:
    """Options-flow reload mid-day: the sensor restores, then demand starts counting."""
    freezer.move_to(_utc(2026, 8, 12, 6, 0))  # 16:00 local
    extra = {  # a sensor that has been running today, with no demand state yet
        "native_value": 1.0,
        "native_unit_of_measurement": "AUD",
        "energy_total": 0.1,
        "supply_total": 0.9,
        "baseline_kwh": 1000.0,
        "energy_ts": _utc(2026, 8, 12, 6, 0).isoformat(),  # exactly at the window start
        "supply_date": "2026-08-12",
    }
    mock_restore_cache_with_extra_data(hass, ((State(COST, "1.0"), extra),))
    hass.states.async_set(IMPORT, "1000", _energy())
    await _setup(
        hass,
        options={
            CONF_DEMAND_ENABLED: True,
            CONF_DEMAND_RATE: 20.0,
            CONF_DEMAND_BASIS: "day",
            CONF_DEMAND_START: "16:00",
            CONF_DEMAND_END: "21:00",
            CONF_DEMAND_DAYS: DAYS,
        },
    )
    freezer.move_to(_utc(2026, 8, 12, 6, 30))
    await _step(hass, IMPORT, "1001.5")  # 3 kW over the first half hour
    freezer.move_to(_utc(2026, 8, 12, 6, 30, 10))
    async_fire_time_changed(hass, _utc(2026, 8, 12, 6, 30, 10))
    await hass.async_block_till_done()
    attrs = hass.states.get(COST).attributes
    assert attrs["peak_demand_kw"] == pytest.approx(3.0)
    assert attrs["demand_charge"] == pytest.approx(0.20 * 3.0)  # charged for today


async def test_public_holiday_takes_weekend_rates(hass: HomeAssistant, freezer) -> None:
    weekday_weekend = [
        {"name": "Weekday", "start": "00:00", "end": "00:00", "rate": 30.0, "days": [0, 1, 2, 3, 4]},
        {"name": "Weekend", "start": "00:00", "end": "00:00", "rate": 10.0, "days": [5, 6]},
    ]
    # Mon 8 June 2026 is the King's Birthday holiday in Victoria.
    freezer.move_to(_utc(2026, 6, 8, 2, 0))
    hass.states.async_set(IMPORT, "1", _energy())
    await _setup(
        hass,
        periods=weekday_weekend,
        options={CONF_HOLIDAY_STATE: "VIC", CONF_HOLIDAYS_AS: "weekend"},
    )
    assert hass.states.get("sensor.test_current_tariff").state == "Weekend"

    # A normal Monday a week later is a weekday.
    freezer.move_to(_utc(2026, 6, 15, 2, 0))
    async_fire_time_changed(hass, _utc(2026, 6, 15, 2, 0))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.test_current_tariff").state == "Weekday"


async def test_holidays_ignored_unless_enabled(hass: HomeAssistant, freezer) -> None:
    weekday_weekend = [
        {"name": "Weekday", "start": "00:00", "end": "00:00", "rate": 30.0, "days": [0, 1, 2, 3, 4]},
        {"name": "Weekend", "start": "00:00", "end": "00:00", "rate": 10.0, "days": [5, 6]},
    ]
    freezer.move_to(_utc(2026, 6, 8, 2, 0))
    hass.states.async_set(IMPORT, "1", _energy())
    await _setup(hass, periods=weekday_weekend)  # no holiday state chosen
    assert hass.states.get("sensor.test_current_tariff").state == "Weekday"


# -- live sensors ------------------------------------------------------------------


async def test_live_sensors(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))  # 16:00 local, peak
    hass.states.async_set(IMPORT, "1", _energy())
    hass.states.async_set(EXPORT, "1", _energy())
    await _setup(hass, supply=SUPPLY, export=True)
    assert hass.states.get("sensor.test_current_tariff").state == "Peak"
    assert float(hass.states.get("sensor.test_current_import_price").state) == pytest.approx(
        PEAK / 100
    )
    assert float(hass.states.get("sensor.test_supply_charge_today").state) == pytest.approx(
        SUPPLY / 100
    )
    assert float(hass.states.get("sensor.test_current_feed_in_price").state) == pytest.approx(
        FEED_IN / 100
    )


# -- restore -----------------------------------------------------------------


async def test_restore_keeps_totals_and_catches_up_downtime(hass: HomeAssistant, freezer) -> None:
    """A restart must not lose money, double count, or skip supply days."""
    freezer.move_to(_utc(2026, 8, 12, 6, 0))  # 16:00 local on 12 Aug
    extra = {
        "native_value": 10.0,
        "native_unit_of_measurement": "AUD",
        "energy_total": 9.0,
        "supply_total": 1.0,
        "baseline_kwh": 1000.0,
        "energy_ts": _utc(2026, 8, 12, 5, 0).isoformat(),  # last reading 1h ago
        "supply_date": "2026-08-10",  # two local days ago
    }
    mock_restore_cache_with_extra_data(hass, ((State(COST, "10.0"), extra),))
    hass.states.async_set(IMPORT, "1002", _energy())  # +2 kWh while HA was down
    await _setup(hass, supply=SUPPLY)

    supply_gap = 2 * SUPPLY / 100
    energy_gap = 2 * SOAK / 100  # 15:00-16:00 local is the solar-soak window
    assert _value(hass, COST) == pytest.approx(10.0 + supply_gap + energy_gap, abs=1e-4)


async def test_restart_on_the_same_day_does_not_charge_supply_twice(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    extra = {
        "native_value": 5.0,
        "native_unit_of_measurement": "AUD",
        "energy_total": 4.0,
        "supply_total": 1.0,
        "baseline_kwh": 100.0,
        "energy_ts": _utc(2026, 8, 12, 5, 59).isoformat(),
        "supply_date": "2026-08-12",  # today already charged
    }
    mock_restore_cache_with_extra_data(hass, ((State(COST, "5.0"), extra),))
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(hass, supply=SUPPLY)
    assert _value(hass, COST) == pytest.approx(5.0)


async def test_restore_without_a_baseline_does_not_bill_the_whole_counter(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    extra = {"native_value": 4.5, "native_unit_of_measurement": "AUD"}
    mock_restore_cache_with_extra_data(hass, ((State(COST, "4.5"), extra),))
    hass.states.async_set(IMPORT, "987654", _energy())  # huge lifetime counter
    await _setup(hass)
    assert _value(hass, COST) == 0.0


async def test_restore_keeps_the_allowance_used_so_far_today(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(_utc(2026, 8, 12, 2, 0))  # 12:00 local on 12 Aug
    step = [{"name": "Step", "start": "00:00", "end": "00:00", "rate": 30.0, "days": DAYS,
             "step_kwh": 10.0, "rate_after": 40.0}]
    extra = {
        "native_value": 3.0,
        "native_unit_of_measurement": "AUD",
        "energy_total": 3.0,
        "baseline_kwh": 100.0,
        "energy_ts": _utc(2026, 8, 12, 1, 0).isoformat(),
        "supply_date": "2026-08-12",
        "blocks": {"key": "2026-08-12", "used": {"Step": 9.0}},  # 9 of 10 kWh used
    }
    mock_restore_cache_with_extra_data(hass, ((State(COST, "3.0"), extra),))
    hass.states.async_set(IMPORT, "103", _energy())  # +3 kWh while down
    await _setup(hass, periods=step)
    # 1 kWh left in the allowance at 30c, then 2 kWh at 40c.
    assert _value(hass, COST) == pytest.approx(3.0 + 0.30 + 2 * 0.40)


async def test_restore_keeps_the_demand_maximum(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    extra = {
        "native_value": 1.0,
        "native_unit_of_measurement": "AUD",
        "demand_total": 1.0,
        "baseline_kwh": 100.0,
        "energy_ts": _utc(2026, 8, 12, 5, 59).isoformat(),
        "supply_date": "2026-08-12",
        "demand": {"month": "2026-08", "max_kw": 5.0, "days": 12, "interval": None, "interval_kwh": 0.0},
    }
    mock_restore_cache_with_extra_data(hass, ((State(COST, "1.0"), extra),))
    hass.states.async_set(IMPORT, "100", _energy())
    await _setup(
        hass,
        options={
            CONF_DEMAND_ENABLED: True,
            CONF_DEMAND_RATE: 20.0,
            CONF_DEMAND_BASIS: "day",
            CONF_DEMAND_START: "16:00",
            CONF_DEMAND_END: "21:00",
            CONF_DEMAND_DAYS: DAYS,
        },
    )
    assert hass.states.get(COST).attributes["peak_demand_kw"] == pytest.approx(5.0)


async def test_save_and_restore_round_trip(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_utc(2026, 8, 12, 6, 0))
    hass.states.async_set(IMPORT, "100", _energy())
    entry = await _setup(hass, supply=SUPPLY)
    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "101")
    value = _value(hass, COST)

    entity = next(e for e in hass.data["sensor"].entities if e.entity_id == COST)
    stored = entity.extra_restore_state_data.as_dict()
    assert stored["baseline_kwh"] == 101.0
    assert stored["supply_date"] == "2026-08-12"
    assert stored["energy_total"] + stored["supply_total"] == pytest.approx(value)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_restore_cache_with_extra_data(hass, ((State(COST, str(value)), stored),))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _value(hass, COST) == pytest.approx(value)  # supply not charged twice

    freezer.tick(timedelta(minutes=1))
    await _step(hass, IMPORT, "102")
    assert _value(hass, COST) == pytest.approx(value + PEAK / 100)


# -- config flow ---------------------------------------------------------------

ALL_DAYS = [str(d) for d in range(7)]


def _period(name, start, end, rate, more=False, **extra):
    return {
        "name": name,
        "start": start,
        "end": end,
        "rate": rate,
        "days": ALL_DAYS,
        "add_another": more,
        **extra,
    }


async def _start_flow(
    hass: HomeAssistant,
    template: str,
    *,
    price_unit="cents",
    export: bool = False,
    controlled_load: bool = False,
):
    """Run the wizard up to (and including) choosing the usage template."""
    hass.states.async_set(IMPORT, "1", _energy())
    hass.states.async_set(EXPORT, "1", _energy())
    hass.states.async_set(CL, "1", _energy())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    user = {"name": "Home", CONF_IMPORT_ENTITY: IMPORT, CONF_CURRENCY: "aud"}
    if export:
        user[CONF_EXPORT_ENTITY] = EXPORT
    if controlled_load:
        user[CONF_CL_ENTITY] = CL
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["step_id"] == "bill"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PRICE_UNIT: price_unit,
            CONF_RATES_INCLUDE_GST: True,
            CONF_DISCOUNT_PERCENT: 5,
            CONF_DISCOUNT_SCOPE: "usage_and_supply",
            CONF_WINDOW_TIME: "standard",
            CONF_HOLIDAY_STATE: "VIC",
            CONF_HOLIDAYS_AS: "weekend",
        },
    )
    assert result["step_id"] == "charges"
    assert result["description_placeholders"]["day_unit"] == (
        "$/day" if price_unit == "dollars" else "c/day"
    )
    charges = {CONF_SUPPLY_CHARGE: SUPPLY, CONF_EXTRA_DAILY: 0}
    if controlled_load:
        charges[CONF_CL_SUPPLY_CHARGE] = 1.1
    result = await hass.config_entries.flow.async_configure(result["flow_id"], charges)
    assert result["step_id"] == "structure"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"template": template}
    )


async def _finish_extras(hass, result, *, demand: bool = False):
    assert result["step_id"] == "extras"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEMAND_ENABLED: demand, CONF_STEP_RESET_DAYS: 1}
    )


async def test_template_wizard_prefills_windows_and_creates_entry(hass: HomeAssistant) -> None:
    result = await _start_flow(hass, "solar_soak")
    assert result["step_id"] == "periods"
    assert result["description_placeholders"]["progress"] == "Rate 1 of 3"
    schema = {str(k): k for k in result["data_schema"].schema}
    assert schema["name"].default() == "Peak"
    assert schema["start"].default() == "16:00:00"
    assert schema["end"].default() == "21:00:00"

    # Even with "add another" unticked the wizard keeps going until the
    # template's windows are all filled in.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Peak", "16:00:00", "21:00:00", PEAK)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["progress"] == "Rate 2 of 3"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Off-peak", "21:00:00", "11:00:00", OFF_PEAK)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Solar soak", "11:00:00", "16:00:00", SOAK)
    )
    result = await _finish_extras(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {
        CONF_IMPORT_ENTITY: IMPORT,
        CONF_CURRENCY: "AUD",
        CONF_PRICE_UNIT: "cents",
    }
    options = result["options"]
    assert [p["name"] for p in options[CONF_PERIODS]] == ["Peak", "Off-peak", "Solar soak"]
    assert options[CONF_SUPPLY_CHARGE] == SUPPLY
    assert options[CONF_DISCOUNT_PERCENT] == 5
    assert options[CONF_DISCOUNT_SCOPE] == "usage_and_supply"
    assert options[CONF_WINDOW_TIME] == "standard"
    assert options[CONF_HOLIDAY_STATE] == "VIC"
    assert options[CONF_HOLIDAYS_AS] == "weekend"
    assert options[CONF_DEMAND_ENABLED] is False
    await hass.async_block_till_done()


async def test_flat_template_finishes_after_one_rate(hass: HomeAssistant) -> None:
    result = await _start_flow(hass, "flat", price_unit="dollars")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Flat rate", "00:00:00", "00:00:00", 0.31)
    )
    result = await _finish_extras(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRICE_UNIT] == "dollars"
    assert result["options"][CONF_PERIODS][0]["rate"] == 0.31
    await hass.async_block_till_done()


async def test_step_rates_are_collected_in_the_wizard(hass: HomeAssistant) -> None:
    result = await _start_flow(hass, "step")
    # An allowance without the price after it is rejected.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _period("Step rate", "00:00:00", "00:00:00", 30.0, step_kwh=11),
    )
    assert result["errors"]["rate_after"] == "step_needs_rate"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _period("Step rate", "00:00:00", "00:00:00", 30.0, step_kwh=11, rate_after=36.0),
    )
    result = await _finish_extras(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    period = result["options"][CONF_PERIODS][0]
    assert (period["rate"], period["step_kwh"], period["rate_after"]) == (30.0, 11.0, 36.0)
    await hass.async_block_till_done()


async def test_gap_is_reported_for_a_custom_structure(hass: HomeAssistant) -> None:
    result = await _start_flow(hass, "custom")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Peak", "16:00:00", "21:00:00", PEAK)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "gap"


async def test_overlap_is_rejected(hass: HomeAssistant) -> None:
    result = await _start_flow(hass, "custom")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("All day", "00:00:00", "00:00:00", 10, more=True)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Extra", "10:00:00", "12:00:00", 20)
    )
    assert result["errors"]["base"] == "overlap"


async def test_start_over_restores_the_template(hass: HomeAssistant) -> None:
    result = await _start_flow(hass, "peak_offpeak")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Peak", "07:00:00", "23:00:00", PEAK)
    )
    assert result["description_placeholders"]["progress"] == "Rate 2 of 2"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_period("x", "00:00:00", "00:00:00", 0), "start_over": True}
    )
    assert result["description_placeholders"]["progress"] == "Rate 1 of 2"


async def test_full_wizard_with_solar_controlled_load_and_demand(hass: HomeAssistant) -> None:
    result = await _start_flow(hass, "flat", export=True, controlled_load=True)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Flat rate", "00:00:00", "00:00:00", 30.0)
    )
    # Feed-in comes next because there is an export sensor.
    assert result["step_id"] == "feed_in_structure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"template": "feed_tiered"}
    )
    assert result["step_id"] == "feed_in_periods"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _period("Feed-in", "00:00:00", "00:00:00", 10.0, step_kwh=8, rate_after=3.0),
    )
    # Then controlled load.
    assert result["step_id"] == "cl_structure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"template": "cl_flat"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Controlled load", "00:00:00", "00:00:00", 18.0)
    )
    # Finally the extras, with a demand charge.
    result = await _finish_extras(hass, result, demand=True)
    assert result["step_id"] == "demand"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_DEMAND_RATE: 20.0,
            CONF_DEMAND_BASIS: "day",
            CONF_DEMAND_START: "16:00:00",
            CONF_DEMAND_END: "21:00:00",
            CONF_DEMAND_DAYS: ALL_DAYS,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EXPORT_ENTITY] == EXPORT
    assert result["data"][CONF_CL_ENTITY] == CL
    options = result["options"]
    assert options[CONF_FEED_IN_PERIODS][0]["step_kwh"] == 8.0
    assert options[CONF_CL_PERIODS][0]["rate"] == 18.0
    assert options[CONF_CL_SUPPLY_CHARGE] == 1.1
    assert options[CONF_DEMAND_ENABLED] is True
    assert options[CONF_DEMAND_START] == "16:00"
    assert options[CONF_DEMAND_DAYS] == DAYS
    await hass.async_block_till_done()


# -- options flow -------------------------------------------------------------


async def test_options_flow_edits_charges(hass: HomeAssistant) -> None:
    hass.states.async_set(IMPORT, "1", _energy())
    entry = await _setup(hass, supply=SUPPLY)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    # Feed-in and controlled load are not offered without those sensors.
    assert "feed_in_structure" not in result["menu_options"]
    assert "cl_structure" not in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "general"}
    )
    assert result["step_id"] == "general"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SUPPLY_CHARGE: 100.0,
            CONF_EXTRA_DAILY: 0,
            CONF_RATES_INCLUDE_GST: False,
            CONF_DISCOUNT_PERCENT: 0,
            CONF_DISCOUNT_SCOPE: "usage",
            CONF_WINDOW_TIME: "standard",
            CONF_HOLIDAY_STATE: "NSW",
            CONF_HOLIDAYS_AS: "weekend",
            CONF_MAX_POWER_KW: 60,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_WINDOW_TIME] == "standard"
    assert entry.options[CONF_HOLIDAY_STATE] == "NSW"
    assert entry.options[CONF_SUPPLY_CHARGE] == 100.0
    assert entry.options[CONF_RATES_INCLUDE_GST] is False
    assert entry.options[CONF_PERIODS] == PERIODS  # untouched
    await hass.async_block_till_done()


async def test_options_flow_can_re_enter_feed_in(hass: HomeAssistant) -> None:
    hass.states.async_set(IMPORT, "1", _energy())
    hass.states.async_set(EXPORT, "1", _energy())
    entry = await _setup(hass, export=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "feed_in_structure" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "feed_in_structure"}
    )
    assert "Feed-in" in result["description_placeholders"]["saved"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"template": "feed_tiered"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _period("Feed-in", "00:00:00", "00:00:00", 12.0, step_kwh=10, rate_after=4.0),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    saved = entry.options[CONF_FEED_IN_PERIODS][0]
    assert (saved["rate"], saved["step_kwh"], saved["rate_after"]) == (12.0, 10.0, 4.0)
    assert entry.options[CONF_PERIODS] == PERIODS  # usage rates untouched
    await hass.async_block_till_done()


async def test_options_flow_can_add_a_demand_charge(hass: HomeAssistant) -> None:
    hass.states.async_set(IMPORT, "1", _energy())
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "extras"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEMAND_ENABLED: True, CONF_STEP_RESET_DAYS: 1}
    )
    assert result["step_id"] == "demand"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DEMAND_RATE: 700.0,
            CONF_DEMAND_BASIS: "month",
            CONF_DEMAND_START: "17:00:00",
            CONF_DEMAND_END: "20:00:00",
            CONF_DEMAND_DAYS: ["0", "1", "2", "3", "4"],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DEMAND_ENABLED] is True
    assert entry.options[CONF_DEMAND_BASIS] == "month"
    assert entry.options[CONF_DEMAND_DAYS] == [0, 1, 2, 3, 4]
    await hass.async_block_till_done()


def _template_default(result) -> str:
    keys = {str(k): k for k in result["data_schema"].schema}
    return keys["template"].default()


async def test_options_picker_preselects_the_saved_structure(hass: HomeAssistant) -> None:
    hass.states.async_set(IMPORT, "1", _energy())
    entry = await _setup(hass)  # saved usage windows match the solar-soak template
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "structure"}
    )
    assert _template_default(result) == "solar_soak"


async def test_options_picker_preselects_feed_in_and_custom(hass: HomeAssistant) -> None:
    hass.states.async_set(IMPORT, "1", _energy())
    hass.states.async_set(EXPORT, "1", _energy())
    tiered = [{"name": "Feed-in", "start": "00:00", "end": "00:00", "rate": 10.0,
               "days": DAYS, "step_kwh": 8.0, "rate_after": 3.0}]
    weekday_weekend = [
        {"name": "Weekday", "start": "00:00", "end": "00:00", "rate": 30.0, "days": [0, 1, 2, 3, 4]},
        {"name": "Weekend", "start": "00:00", "end": "00:00", "rate": 10.0, "days": [5, 6]},
    ]
    entry = await _setup(hass, export=True, feed_in_periods=tiered, periods=weekday_weekend)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "feed_in_structure"}
    )
    assert _template_default(result) == "feed_tiered"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "structure"}
    )
    assert _template_default(result) == "custom"  # day-specific rates


async def test_setup_wizard_still_defaults_to_flat(hass: HomeAssistant) -> None:
    """A fresh wizard has nothing saved, so the picker offers the simplest default."""
    hass.states.async_set(IMPORT, "1", _energy())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Home", CONF_IMPORT_ENTITY: IMPORT, CONF_CURRENCY: "aud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_UNIT: "cents", CONF_RATES_INCLUDE_GST: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SUPPLY_CHARGE: SUPPLY, CONF_EXTRA_DAILY: 0}
    )
    assert result["step_id"] == "structure"
    assert _template_default(result) == "flat"


async def test_rate_fields_start_at_zero_not_at_the_negative_minimum(hass: HomeAssistant) -> None:
    """HA shows a number box's minimum when there is no default: -10000 for feed-in."""
    result = await _start_flow(hass, "flat", export=True)
    keys = {str(k): k for k in result["data_schema"].schema}
    assert keys["rate"].default() == 0
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _period("Flat rate", "00:00:00", "00:00:00", 30.0)
    )
    assert result["step_id"] == "feed_in_structure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"template": "feed_flat"}
    )
    keys = {str(k): k for k in result["data_schema"].schema}
    assert result["step_id"] == "feed_in_periods"
    assert keys["rate"].default() == 0  # not the field's minimum of -10000
