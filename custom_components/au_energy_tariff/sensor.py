"""Sensors for AU Energy Tariff."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import (
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
    CONF_STEP_ANCHOR,
    CONF_STEP_RESET_DAYS,
    CONF_SUPPLY_CHARGE,
    CONF_WINDOW_TIME,
    DEFAULT_CURRENCY,
    DEFAULT_MAX_POWER_KW,
    DEFAULT_PRICE_UNIT,
    DOMAIN,
)
from .tariff import (
    ALL_DAYS,
    MAX_CATCH_UP_DAYS,
    PRICE_UNIT_DIVISORS,
    BlockState,
    DemandCharge,
    DemandState,
    Period,
    Tariff,
    evaluate_reading,
    parse_minutes,
    to_kwh,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# On a sensor's very first run the starting value (0) is published first and the
# first day's charges follow after this many seconds. Long-term statistics start
# from a sensor's first recorded value, so this is what lets them include it.
FIRST_CHARGE_DELAY = 5


def holiday_checker(state: str | None) -> Callable[[date], bool] | None:
    """Return a ``date -> is public holiday`` function for a state, or None."""
    if not state or state == "none":
        return None
    try:
        import holidays  # noqa: PLC0415
    except ImportError:
        _LOGGER.warning("The 'holidays' library is unavailable; ignoring public holidays")
        return None
    calendar = holidays.Australia(subdiv=state)
    return lambda day: day in calendar


@dataclass(frozen=True)
class Tariffs:
    """The three schedules an entry can price."""

    usage: Tariff
    feed_in: Tariff
    controlled_load: Tariff


def build_tariffs(
    entry: ConfigEntry, holiday_check: Callable[[date], bool] | None = None
) -> Tariffs:
    """Build the usage, feed-in and controlled-load tariffs from an entry."""
    options = entry.options
    price_unit = entry.data.get(CONF_PRICE_UNIT, DEFAULT_PRICE_UNIT)
    anchor_raw = options.get(CONF_STEP_ANCHOR)

    shared: dict[str, Any] = {
        "tz": dt_util.get_default_time_zone(),
        "rate_divisor": PRICE_UNIT_DIVISORS.get(price_unit, 100.0),
        "standard_time_windows": options.get(CONF_WINDOW_TIME, "local") == "standard",
        "holiday_check": holiday_check,
        "holidays_as_weekend": options.get(CONF_HOLIDAYS_AS, "normal") == "weekend",
        "step_reset_days": int(options.get(CONF_STEP_RESET_DAYS, 1) or 1),
        "step_anchor": date.fromisoformat(anchor_raw) if anchor_raw else None,
    }
    billed: dict[str, Any] = {
        "rates_include_gst": bool(options.get(CONF_RATES_INCLUDE_GST, True)),
        "discount_percent": float(options.get(CONF_DISCOUNT_PERCENT, 0.0)),
        "discount_on_supply": options.get(CONF_DISCOUNT_SCOPE, "usage") == "usage_and_supply",
    }

    def periods(key: str) -> tuple[Period, ...]:
        return tuple(Period.from_dict(p) for p in options.get(key, []))

    demand = None
    if options.get(CONF_DEMAND_ENABLED):
        window = Period(
            "Demand window",
            parse_minutes(options.get(CONF_DEMAND_START, "16:00")),
            parse_minutes(options.get(CONF_DEMAND_END, "21:00")),
            0.0,
            tuple(int(d) for d in options.get(CONF_DEMAND_DAYS, ALL_DAYS)),
        )
        demand = DemandCharge(
            float(options.get(CONF_DEMAND_RATE, 0.0)),
            options.get(CONF_DEMAND_BASIS, "day"),
            window,
        )

    return Tariffs(
        usage=Tariff(
            periods=periods(CONF_PERIODS),
            supply_charge=float(options.get(CONF_SUPPLY_CHARGE, 0.0)),
            extra_daily=float(options.get(CONF_EXTRA_DAILY, 0.0)),
            demand=demand,
            **shared,
            **billed,
        ),
        # Feed-in is entered as paid: no GST added, no discount.
        feed_in=Tariff(
            periods=periods(CONF_FEED_IN_PERIODS),
            supply_charge=0.0,
            **shared,
        ),
        controlled_load=Tariff(
            periods=periods(CONF_CL_PERIODS),
            supply_charge=float(options.get(CONF_CL_SUPPLY_CHARGE, 0.0)),
            **shared,
            **billed,
        ),
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="AU Energy Tariff",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors for a config entry."""
    holiday = await hass.async_add_executor_job(
        holiday_checker, entry.options.get(CONF_HOLIDAY_STATE)
    )
    tariffs = build_tariffs(entry, holiday)
    currency = entry.data.get(CONF_CURRENCY, DEFAULT_CURRENCY)
    max_power = float(entry.options.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW))

    entities: list[SensorEntity] = [
        _CumulativeSensor(
            entry, "import_cost", tariffs.usage, currency,
            entry.data[CONF_IMPORT_ENTITY], max_power,
        ),
        *(
            LiveSensor(entry, tariffs.usage, description)
            for description in _live_descriptions(currency)
        ),
    ]
    if export_entity := entry.data.get(CONF_EXPORT_ENTITY):
        entities.append(
            _CumulativeSensor(
                entry, "export_credit", tariffs.feed_in, currency, export_entity, max_power
            )
        )
        entities.append(
            LiveSensor(entry, tariffs.feed_in, _feed_in_price_description(currency))
        )
    if cl_entity := entry.data.get(CONF_CL_ENTITY):
        entities.append(
            _CumulativeSensor(
                entry, "controlled_load_cost", tariffs.controlled_load, currency,
                cl_entity, max_power,
            )
        )
    async_add_entities(entities)


# --------------------------------------------------------------------------
# Cumulative cost / credit sensors
# --------------------------------------------------------------------------


@dataclass
class _TariffExtraStoredData(SensorExtraStoredData):
    """Restore data: running totals, meter baseline and allowance/demand state."""

    energy_total: float = 0.0
    supply_total: float = 0.0
    demand_total: float = 0.0
    baseline_kwh: float | None = None
    energy_ts: str | None = None
    supply_date: str | None = None  # last local date whose charges were added
    blocks: dict[str, Any] | None = None
    demand: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly dict."""
        data = super().as_dict()
        data.update(
            energy_total=self.energy_total,
            supply_total=self.supply_total,
            demand_total=self.demand_total,
            baseline_kwh=self.baseline_kwh,
            energy_ts=self.energy_ts,
            supply_date=self.supply_date,
            blocks=self.blocks,
            demand=self.demand,
        )
        return data


class _CumulativeSensor(RestoreSensor):
    """A monotonic-ish running total fed by a cumulative energy sensor.

    Used for import cost (with supply and demand), export credit (feed-in) and
    controlled-load cost. What differs is the tariff it prices with.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.MONETARY
    # Home Assistant only permits ``total`` for the monetary device class.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        entry: ConfigEntry,
        key: str,
        tariff: Tariff,
        currency: str,
        source_entity: str,
        max_power_kw: float,
    ) -> None:
        self._tariff = tariff
        self._source_entity = source_entity
        self._max_power_kw = max_power_kw
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_native_unit_of_measurement = currency
        self._attr_device_info = _device_info(entry)

        self._energy_total = 0.0
        self._supply_total = 0.0
        self._demand_total = 0.0
        self._baseline_kwh: float | None = None
        self._energy_ts: datetime | None = None  # last accepted reading
        self._supply_date: date | None = None  # last local day charged
        self._blocks = BlockState()
        self._demand_state = DemandState()
        self._warned_unit = False

    # -- state -------------------------------------------------------------------

    @property
    def native_value(self) -> float:
        """Return the running total."""
        return round(self._energy_total + self._supply_total + self._demand_total, 6)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the breakdown (handy for reconciling with a bill)."""
        attrs: dict[str, Any] = {"energy": round(self._energy_total, 6)}
        if self._tariff.daily_supply_cost:
            attrs["supply_charge"] = round(self._supply_total, 6)
        if self._tariff.demand is not None:
            attrs["demand_charge"] = round(self._demand_total, 6)
            attrs["peak_demand_kw"] = round(self._demand_state.max_kw, 3)
        return attrs

    @property
    def extra_restore_state_data(self) -> _TariffExtraStoredData:
        """Persist totals, the meter baseline and allowance/demand state."""
        return _TariffExtraStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            self._energy_total,
            self._supply_total,
            self._demand_total,
            self._baseline_kwh,
            self._energy_ts.isoformat() if self._energy_ts else None,
            self._supply_date.isoformat() if self._supply_date else None,
            self._blocks.to_dict(),
            self._demand_state.to_dict(),
        )

    async def async_added_to_hass(self) -> None:
        """Restore state, catch up on downtime, then start tracking."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_extra_data()
        self._restore(restored)
        first_run = restored is None

        now = dt_util.utcnow()
        if not first_run:
            self._charge_supply_days(now)  # catches up any days missed while down
        self._handle_state(self.hass.states.get(self._source_entity), now)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source_entity], self._async_source_changed
            )
        )
        # Daily charges are flat amounts, added at local midnight.
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._async_new_day, hour=0, minute=0, second=0
            )
        )
        if self._tariff.demand is not None:
            # Close each 30 minute demand interval promptly.
            self.async_on_remove(
                async_track_time_change(
                    self.hass, self._async_flush_demand, minute=[0, 30], second=10
                )
            )
        self.async_write_ha_state()
        if first_run:
            # The starting value has been published; the first day's charges
            # come next so they land inside the long-term statistics.
            self.async_on_remove(
                async_call_later(self.hass, FIRST_CHARGE_DELAY, self._async_first_charge)
            )

    def _restore(self, extra: Any) -> None:
        if extra is None:
            return
        data = extra.as_dict()
        try:
            self._energy_total = float(data.get("energy_total", 0.0))
            self._supply_total = float(data.get("supply_total", 0.0))
            self._demand_total = float(data.get("demand_total", 0.0))
            baseline = data.get("baseline_kwh")
            self._baseline_kwh = None if baseline is None else float(baseline)
            self._energy_ts = _parse_ts(data.get("energy_ts"))
            raw_date = data.get("supply_date")
            self._supply_date = date.fromisoformat(raw_date) if raw_date else None
            self._blocks = BlockState.from_dict(data.get("blocks"))
            self._demand_state = DemandState.from_dict(data.get("demand"))
        except (TypeError, ValueError):
            _LOGGER.warning("Could not restore %s; starting from zero", self.entity_id)
            self._energy_total = self._supply_total = self._demand_total = 0.0
            self._baseline_kwh = self._energy_ts = self._supply_date = None
            self._blocks, self._demand_state = BlockState(), DemandState()

    # -- updates ----------------------------------------------------------------

    def _charge_supply_days(self, now: datetime) -> None:
        """Add the flat daily charges for each local day not yet charged.

        On first run the current day is charged straight away (the charge is
        for the whole day), and after downtime every missed day is caught up.
        """
        self._demand_total += self._tariff.demand_flush(self._demand_state, now)
        today = dt_util.as_local(now).date()
        last = self._supply_date or (today - timedelta(days=1))
        days = min((today - last).days, MAX_CATCH_UP_DAYS)
        for offset in range(1, days + 1):
            day = last + timedelta(days=offset)
            self._supply_total += self._tariff.daily_supply_cost
            self._demand_total += self._tariff.demand_daily(self._demand_state, day)
        if days > 0 or self._supply_date is None:
            self._supply_date = today

    def _handle_state(self, state: State | None, now: datetime) -> None:
        """Fold a source reading into the totals."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return  # keep baseline; the gap is apportioned when it returns
        try:
            value = float(state.state)
        except ValueError:
            return
        if not math.isfinite(value):
            return
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        kwh = to_kwh(value, unit)
        if kwh is None:
            if not self._warned_unit:
                _LOGGER.warning(
                    "%s has unsupported unit %r (need Wh, kWh, MWh or GWh)",
                    self._source_entity,
                    unit,
                )
                self._warned_unit = True
            return

        elapsed = (now - self._energy_ts).total_seconds() if self._energy_ts else 0.0
        delta, baseline, reason = evaluate_reading(
            self._baseline_kwh, kwh, elapsed, self._max_power_kw
        )
        if reason == "reset":
            _LOGGER.warning(
                "%s went backwards (%.3f -> %.3f kWh); treating as a meter reset",
                self._source_entity,
                self._baseline_kwh,
                kwh,
            )
        elif reason == "jump":
            _LOGGER.warning(
                "%s jumped %.3f kWh in %.0fs, above the %.0f kW limit; ignoring",
                self._source_entity,
                kwh - (self._baseline_kwh or 0.0),
                elapsed,
                self._max_power_kw,
            )
        if delta > 0 and self._energy_ts is not None:
            self._energy_total += self._tariff.energy_cost(
                self._energy_ts, now, delta, self._blocks
            )
            self._demand_total += self._tariff.demand_register(
                self._demand_state, self._energy_ts, now, delta
            )

        self._baseline_kwh = baseline
        if reason in ("initial", "ok", "reset", "jump"):
            self._energy_ts = now

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        now = dt_util.utcnow()
        self._charge_supply_days(now)
        self._handle_state(event.data["new_state"], now)
        self.async_write_ha_state()

    @callback
    def _async_first_charge(self, _now: datetime) -> None:
        self._charge_supply_days(dt_util.utcnow())
        self.async_write_ha_state()

    @callback
    def _async_new_day(self, _now: datetime) -> None:
        self._charge_supply_days(dt_util.utcnow())
        self.async_write_ha_state()

    @callback
    def _async_flush_demand(self, _now: datetime) -> None:
        self._demand_total += self._tariff.demand_flush(
            self._demand_state, dt_util.utcnow()
        )
        self.async_write_ha_state()


def _parse_ts(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


# --------------------------------------------------------------------------
# Live (stateless) sensors
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class _LiveDescription(SensorEntityDescription):
    value_fn: Callable[[Tariff, datetime], Any]


def _supply_today(tariff: Tariff, now: datetime) -> float:
    """Today's fixed daily charge: already added at midnight."""
    return round(tariff.daily_supply_cost, 6)


def _live_descriptions(currency: str) -> list[_LiveDescription]:
    return [
        _LiveDescription(
            key="current_period",
            translation_key="current_period",
            value_fn=lambda t, now: (p.name if (p := t.period_at(now)) else None),
        ),
        _LiveDescription(
            key="current_price",
            translation_key="current_price",
            native_unit_of_measurement=f"{currency}/kWh",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=5,
            value_fn=lambda t, now: round(t.price_at(now), 6),
        ),
        _LiveDescription(
            key="supply_charge_today",
            translation_key="supply_charge_today",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=currency,
            suggested_display_precision=2,
            value_fn=_supply_today,
        ),
    ]


def _feed_in_price_description(currency: str) -> _LiveDescription:
    return _LiveDescription(
        key="current_feed_in_price",
        translation_key="current_feed_in_price",
        native_unit_of_measurement=f"{currency}/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        value_fn=lambda t, now: round(t.price_at(now), 6),
    )


class LiveSensor(SensorEntity):
    """A sensor computed from a tariff and the clock, refreshed each minute."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: _LiveDescription

    def __init__(
        self,
        entry: ConfigEntry,
        tariff: Tariff,
        description: _LiveDescription,
    ) -> None:
        self.entity_description = description
        self._tariff = tariff
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Any:
        """Return the value for the current time."""
        return self.entity_description.value_fn(self._tariff, dt_util.utcnow())

    async def async_added_to_hass(self) -> None:
        """Refresh at the top of every minute."""
        self.async_on_remove(
            async_track_time_change(self.hass, self._async_tick, second=0)
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.async_write_ha_state()
