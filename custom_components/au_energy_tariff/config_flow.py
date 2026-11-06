"""Config flow for AU Energy Tariff.

A guided wizard that mirrors how an Australian electricity bill is laid out:

1. which meter sensors to price (grid import, solar export, controlled load),
2. how the bill prints prices (cents or dollars), GST, discounts, time basis
   and public holidays,
3. the daily supply charge,
4. the usage rate structure: pick a typical template (window names and times
   are pre-filled) and just type in the prices, or build a custom one,
5. the feed-in structure (flat, tiered or time-varying) if you export,
6. controlled-load rates if you have a controlled load register,
7. optional extras: allowance reset period and a demand charge.

Steps that do not apply to your setup are skipped.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADD_ANOTHER,
    CONF_CL_ENTITY,
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
    CONF_HOLIDAY_STATE,
    CONF_HOLIDAYS_AS,
    CONF_IMPORT_ENTITY,
    CONF_MAX_POWER_KW,
    CONF_PERIOD_DAYS,
    CONF_PERIOD_END,
    CONF_PERIOD_NAME,
    CONF_PERIOD_RATE,
    CONF_PERIOD_RATE_AFTER,
    CONF_PERIOD_START,
    CONF_PERIOD_STEP_KWH,
    CONF_PRICE_UNIT,
    CONF_RATES_INCLUDE_GST,
    CONF_START_OVER,
    CONF_STEP_ANCHOR,
    CONF_STEP_RESET_DAYS,
    CONF_SUPPLY_CHARGE,
    CONF_TEMPLATE,
    CONF_WINDOW_TIME,
    DEFAULT_CURRENCY,
    DEFAULT_MAX_POWER_KW,
    DEFAULT_NAME,
    DEFAULT_PRICE_UNIT,
    DOMAIN,
    HOLIDAY_STATES,
    OPTION_KEY_FOR_TARGET,
    TARGET_CL,
    TARGET_FEED_IN,
    TARGET_USAGE,
)
from .tariff import (
    ALL_DAYS,
    TARGET_TEMPLATES,
    TEMPLATES,
    Period,
    format_minutes,
    matching_template,
    validate_periods,
)

# Step ids for each schedule.
STRUCTURE_STEP = {
    TARGET_USAGE: "structure",
    TARGET_FEED_IN: "feed_in_structure",
    TARGET_CL: "cl_structure",
}
PERIODS_STEP = {
    TARGET_USAGE: "periods",
    TARGET_FEED_IN: "feed_in_periods",
    TARGET_CL: "cl_periods",
}
TEMPLATE_TRANSLATION = {
    TARGET_USAGE: "template",
    TARGET_FEED_IN: "feed_in_template",
    TARGET_CL: "cl_template",
}
DEFAULT_TEMPLATE = {TARGET_USAGE: "flat", TARGET_FEED_IN: "feed_flat", TARGET_CL: "cl_flat"}

_ENERGY_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="energy")
)
_DAYS = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[str(d) for d in ALL_DAYS],
        multiple=True,
        mode=selector.SelectSelectorMode.LIST,
        translation_key="weekday",
    )
)


def _units(price_unit: str) -> dict[str, str]:
    """Unit labels for the bill's price unit."""
    if price_unit == "dollars":
        return {"kwh_unit": "$/kWh", "day_unit": "$/day", "kw_unit": "$/kW"}
    return {"kwh_unit": "c/kWh", "day_unit": "c/day", "kw_unit": "c/kW"}


def _number(unit: str, *, minimum: float = 0, maximum: float = 10000) -> Any:
    # step "any" lets people type prices to as many decimals as their bill shows.
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step="any",
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _select(options: list[str], key: str, *, dropdown: bool = False) -> Any:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=(
                selector.SelectSelectorMode.DROPDOWN
                if dropdown
                else selector.SelectSelectorMode.LIST
            ),
            translation_key=key,
        )
    )


def _describe(periods: list[dict[str, Any]], kwh_unit: str) -> str:
    """Render periods as a short markdown list for a form description."""
    if not periods:
        return "_none yet_"
    lines = []
    for raw in periods:
        p = Period.from_dict(raw)
        days = "" if p.days == ALL_DAYS else f" (days {','.join(str(d + 1) for d in p.days)})"
        allowance = ""
        if p.step_kwh is not None:
            after = p.rate if p.rate_after is None else p.rate_after
            allowance = f", then {after:g} after {p.step_kwh:g} kWh/day"
        lines.append(
            f"- **{p.name}** {format_minutes(p.start)}-{format_minutes(p.end)}"
            f" at {p.rate:g} {kwh_unit}{allowance}{days}"
        )
    return "\n".join(lines)


def _bill_schema(defaults: dict[str, Any], *, with_unit: bool) -> dict[Any, Any]:
    """Prices, GST, discount, time basis and holiday fields."""
    fields: dict[Any, Any] = {}
    if with_unit:
        fields[vol.Required(CONF_PRICE_UNIT, default=DEFAULT_PRICE_UNIT)] = _select(
            ["cents", "dollars"], "price_unit"
        )
    fields.update(
        {
            vol.Required(
                CONF_RATES_INCLUDE_GST,
                default=defaults.get(CONF_RATES_INCLUDE_GST, True),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_DISCOUNT_PERCENT, default=defaults.get(CONF_DISCOUNT_PERCENT, 0)
            ): _number("%", maximum=100),
            vol.Required(
                CONF_DISCOUNT_SCOPE, default=defaults.get(CONF_DISCOUNT_SCOPE, "usage")
            ): _select(["usage", "usage_and_supply"], "discount_scope"),
            vol.Required(
                CONF_WINDOW_TIME, default=defaults.get(CONF_WINDOW_TIME, "local")
            ): _select(["local", "standard"], "window_time"),
            vol.Required(
                CONF_HOLIDAY_STATE, default=defaults.get(CONF_HOLIDAY_STATE, "none")
            ): _select(HOLIDAY_STATES, "holiday_state", dropdown=True),
            vol.Required(
                CONF_HOLIDAYS_AS, default=defaults.get(CONF_HOLIDAYS_AS, "normal")
            ): _select(["normal", "weekend"], "holidays_as"),
        }
    )
    return fields


def _charges_schema(defaults: dict[str, Any], price_unit: str, *, has_cl: bool) -> dict[Any, Any]:
    units = _units(price_unit)
    fields: dict[Any, Any] = {
        vol.Required(CONF_SUPPLY_CHARGE, default=defaults.get(CONF_SUPPLY_CHARGE, 0.0)): _number(
            units["day_unit"]
        ),
        vol.Required(CONF_EXTRA_DAILY, default=defaults.get(CONF_EXTRA_DAILY, 0.0)): _number(
            units["day_unit"], minimum=-10000
        ),
    }
    if has_cl:
        fields[
            vol.Required(CONF_CL_SUPPLY_CHARGE, default=defaults.get(CONF_CL_SUPPLY_CHARGE, 0.0))
        ] = _number(units["day_unit"])
    return fields


def _extras_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_DEMAND_ENABLED, default=defaults.get(CONF_DEMAND_ENABLED, False)
        ): selector.BooleanSelector(),
        vol.Required(CONF_STEP_RESET_DAYS, default=defaults.get(CONF_STEP_RESET_DAYS, 1)): _number(
            "days", minimum=1, maximum=366
        ),
    }
    anchor = defaults.get(CONF_STEP_ANCHOR)
    fields[
        vol.Optional(
            CONF_STEP_ANCHOR, **({"description": {"suggested_value": anchor}} if anchor else {})
        )
    ] = selector.DateSelector()
    return fields


def _demand_schema(defaults: dict[str, Any], price_unit: str) -> dict[Any, Any]:
    return {
        vol.Required(CONF_DEMAND_RATE, default=defaults.get(CONF_DEMAND_RATE, 0.0)): _number(
            _units(price_unit)["kw_unit"]
        ),
        vol.Required(CONF_DEMAND_BASIS, default=defaults.get(CONF_DEMAND_BASIS, "day")): _select(
            ["day", "month"], "demand_basis"
        ),
        vol.Required(
            CONF_DEMAND_START, default=f"{defaults.get(CONF_DEMAND_START, '16:00')}:00"
        ): selector.TimeSelector(),
        vol.Required(
            CONF_DEMAND_END, default=f"{defaults.get(CONF_DEMAND_END, '21:00')}:00"
        ): selector.TimeSelector(),
        vol.Required(
            CONF_DEMAND_DAYS,
            default=[str(d) for d in defaults.get(CONF_DEMAND_DAYS, ALL_DAYS)],
        ): _DAYS,
    }


class _FlowMixin:
    """Behaviour shared by the setup wizard and the options flow."""

    _target: str
    _periods: list[dict[str, Any]]  # periods entered so far for this schedule
    _queue: list[dict[str, str]]  # pre-filled periods still to enter
    _template: list[dict[str, str]]  # the chosen template, for "start over"
    _schedules: dict[str, list[dict[str, Any]]]  # finished schedules
    _out: dict[str, Any]  # option values gathered so far
    _has_export: bool
    _has_cl: bool

    @property
    def _price_unit(self) -> str:
        raise NotImplementedError

    def _saved(self, target: str) -> list[dict[str, Any]]:
        return []

    async def _async_schedule_done(self, target: str) -> ConfigFlowResult:
        raise NotImplementedError

    async def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    def _defaults(self) -> dict[str, Any]:
        return {}

    # -- template pickers and rate loops, one pair per schedule ----------------

    async def async_step_structure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Usage: pick a typical structure."""
        return await self._structure(TARGET_USAGE, user_input)

    async def async_step_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Usage: enter each rate."""
        return await self._period_form(TARGET_USAGE, user_input)

    async def async_step_feed_in_structure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Feed-in: pick flat, tiered or time-varying."""
        return await self._structure(TARGET_FEED_IN, user_input)

    async def async_step_feed_in_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Feed-in: enter each rate."""
        return await self._period_form(TARGET_FEED_IN, user_input)

    async def async_step_cl_structure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Controlled load: pick a structure."""
        return await self._structure(TARGET_CL, user_input)

    async def async_step_cl_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Controlled load: enter each rate."""
        return await self._period_form(TARGET_CL, user_input)

    async def _structure(self, target: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        step_id = STRUCTURE_STEP[target]
        if user_input is not None:
            self._target = target
            self._template = [
                {"name": n, "start": s, "end": e}
                for n, s, e in TEMPLATES[user_input[CONF_TEMPLATE]]
            ]
            self._queue = [dict(item) for item in self._template]
            self._periods = []
            return await getattr(self, f"async_step_{PERIODS_STEP[target]}")()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TEMPLATE,
                    default=matching_template(target, self._saved(target))
                    or DEFAULT_TEMPLATE[target],
                ): _select(TARGET_TEMPLATES[target], TEMPLATE_TRANSLATION[target])
            }
        )
        kwh_unit = _units(self._price_unit)["kwh_unit"]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id,
            data_schema=schema,
            description_placeholders={"saved": _describe(self._saved(target), kwh_unit)},
        )

    async def _period_form(
        self, target: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        step_id = PERIODS_STEP[target]
        errors: dict[str, str] = {}
        kwh_unit = _units(self._price_unit)["kwh_unit"]

        if user_input is not None:
            if user_input.get(CONF_START_OVER):
                self._periods = []
                self._queue = [dict(item) for item in self._template]
            else:
                days = [int(d) for d in user_input.get(CONF_PERIOD_DAYS, [])]
                step_kwh = user_input.get(CONF_PERIOD_STEP_KWH)
                rate_after = user_input.get(CONF_PERIOD_RATE_AFTER)
                if not days:
                    errors[CONF_PERIOD_DAYS] = "no_days"
                elif not user_input[CONF_PERIOD_NAME].strip():
                    errors[CONF_PERIOD_NAME] = "no_name"
                elif step_kwh and rate_after is None:
                    errors[CONF_PERIOD_RATE_AFTER] = "step_needs_rate"
                else:
                    period: dict[str, Any] = {
                        "name": user_input[CONF_PERIOD_NAME].strip(),
                        "start": user_input[CONF_PERIOD_START][:5],
                        "end": user_input[CONF_PERIOD_END][:5],
                        "rate": float(user_input[CONF_PERIOD_RATE]),
                        "days": sorted(days),
                    }
                    if step_kwh:
                        period["step_kwh"] = float(step_kwh)
                        period["rate_after"] = float(rate_after)
                    self._periods.append(period)
                    if self._queue:
                        self._queue.pop(0)
                    if not (self._queue or user_input[CONF_ADD_ANOTHER]):
                        problem = validate_periods([Period.from_dict(p) for p in self._periods])
                        if problem is None:
                            self._schedules[target] = self._periods
                            return await self._async_schedule_done(target)
                        errors["base"] = problem

        head = self._queue[0] if self._queue else {}
        name_default = {"default": head["name"]} if head else {}
        minimum = -10000 if target == TARGET_FEED_IN else 0  # export charges exist
        schema = vol.Schema(
            {
                vol.Required(CONF_PERIOD_NAME, **name_default): selector.TextSelector(),
                vol.Required(
                    CONF_PERIOD_START, default=f"{head.get('start', '00:00')}:00"
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_PERIOD_END, default=f"{head.get('end', '00:00')}:00"
                ): selector.TimeSelector(),
                # An explicit default: otherwise Home Assistant's number box shows
                # the field's minimum, which for feed-in (negatives allowed) is -10000.
                vol.Required(CONF_PERIOD_RATE, default=0): _number(kwh_unit, minimum=minimum),
                vol.Optional(CONF_PERIOD_STEP_KWH): _number("kWh", maximum=100000),
                vol.Optional(CONF_PERIOD_RATE_AFTER): _number(kwh_unit, minimum=minimum),
                vol.Required(CONF_PERIOD_DAYS, default=[str(d) for d in ALL_DAYS]): _DAYS,
                vol.Required(
                    CONF_ADD_ANOTHER, default=len(self._queue) > 1
                ): selector.BooleanSelector(),
                vol.Optional(CONF_START_OVER, default=False): selector.BooleanSelector(),
            }
        )
        total = len(self._periods) + len(self._queue)
        progress = (
            f"Rate {len(self._periods) + 1} of {total}"
            if self._queue
            else "Add another rate if you need one, or untick the box to finish"
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "progress": progress,
                "periods": _describe(self._periods, kwh_unit),
                "saved": _describe(self._saved(target), kwh_unit),
                "kwh_unit": kwh_unit,
            },
        )

    # -- optional extras: allowance reset and demand charge ---------------------

    async def async_step_extras(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Allowance reset period and whether there is a demand charge."""
        if user_input is not None:
            self._out[CONF_STEP_RESET_DAYS] = int(user_input[CONF_STEP_RESET_DAYS])
            self._out[CONF_STEP_ANCHOR] = user_input.get(CONF_STEP_ANCHOR)
            self._out[CONF_DEMAND_ENABLED] = user_input[CONF_DEMAND_ENABLED]
            if user_input[CONF_DEMAND_ENABLED]:
                return await self.async_step_demand()
            return await self._async_finish()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="extras",
            data_schema=vol.Schema(_extras_schema(self._defaults())),
        )

    async def async_step_demand(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Demand charge: price, basis and measurement window."""
        if user_input is not None:
            self._out.update(
                {
                    CONF_DEMAND_RATE: user_input[CONF_DEMAND_RATE],
                    CONF_DEMAND_BASIS: user_input[CONF_DEMAND_BASIS],
                    CONF_DEMAND_START: user_input[CONF_DEMAND_START][:5],
                    CONF_DEMAND_END: user_input[CONF_DEMAND_END][:5],
                    CONF_DEMAND_DAYS: sorted(int(d) for d in user_input[CONF_DEMAND_DAYS]),
                }
            )
            return await self._async_finish()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="demand",
            data_schema=vol.Schema(_demand_schema(self._defaults(), self._price_unit)),
            description_placeholders=_units(self._price_unit),
        )


class AuEnergyTariffConfigFlow(_FlowMixin, ConfigFlow, domain=DOMAIN):
    """Set up AU Energy Tariff."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._out = {CONF_MAX_POWER_KW: DEFAULT_MAX_POWER_KW}
        self._schedules = {}
        self._periods = []
        self._queue = []
        self._template = []
        self._target = TARGET_USAGE
        self._has_export = False
        self._has_cl = False

    @property
    def _price_unit(self) -> str:
        return self._data.get(CONF_PRICE_UNIT, DEFAULT_PRICE_UNIT)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: which meter sensors to price."""
        if user_input is not None:
            self._data = {
                "name": user_input["name"],
                CONF_IMPORT_ENTITY: user_input[CONF_IMPORT_ENTITY],
                CONF_CURRENCY: user_input[CONF_CURRENCY].strip().upper(),
            }
            if user_input.get(CONF_EXPORT_ENTITY):
                self._data[CONF_EXPORT_ENTITY] = user_input[CONF_EXPORT_ENTITY]
            if user_input.get(CONF_CL_ENTITY):
                self._data[CONF_CL_ENTITY] = user_input[CONF_CL_ENTITY]
            self._has_export = CONF_EXPORT_ENTITY in self._data
            self._has_cl = CONF_CL_ENTITY in self._data
            return await self.async_step_bill()

        schema = vol.Schema(
            {
                vol.Required("name", default=DEFAULT_NAME): selector.TextSelector(),
                vol.Required(CONF_IMPORT_ENTITY): _ENERGY_SENSOR,
                vol.Optional(CONF_EXPORT_ENTITY): _ENERGY_SENSOR,
                vol.Optional(CONF_CL_ENTITY): _ENERGY_SENSOR,
                vol.Required(
                    CONF_CURRENCY,
                    default=self.hass.config.currency or DEFAULT_CURRENCY,
                ): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_bill(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: how the bill prints prices, GST, discounts, time basis."""
        if user_input is not None:
            self._data[CONF_PRICE_UNIT] = user_input.pop(CONF_PRICE_UNIT)
            self._out.update(user_input)
            return await self.async_step_charges()
        return self.async_show_form(
            step_id="bill", data_schema=vol.Schema(_bill_schema({}, with_unit=True))
        )

    async def async_step_charges(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: daily supply charge (and other daily amounts)."""
        if user_input is not None:
            self._out.update(user_input)
            return await self.async_step_structure()
        return self.async_show_form(
            step_id="charges",
            data_schema=vol.Schema(_charges_schema({}, self._price_unit, has_cl=self._has_cl)),
            description_placeholders=_units(self._price_unit),
        )

    async def _async_schedule_done(self, target: str) -> ConfigFlowResult:
        if target == TARGET_USAGE and self._has_export:
            return await self.async_step_feed_in_structure()
        if target in (TARGET_USAGE, TARGET_FEED_IN) and self._has_cl:
            return await self.async_step_cl_structure()
        return await self.async_step_extras()

    async def _async_finish(self) -> ConfigFlowResult:
        options = {
            **self._out,
            **{OPTION_KEY_FOR_TARGET[t]: p for t, p in self._schedules.items()},
        }
        return self.async_create_entry(
            title=self._data["name"],
            data={k: v for k, v in self._data.items() if k != "name"},
            options=options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AuEnergyTariffOptionsFlow:
        """Return the options flow."""
        return AuEnergyTariffOptionsFlow()


class AuEnergyTariffOptionsFlow(_FlowMixin, OptionsFlowWithReload):
    """Edit charges and rates; the entry reloads on save."""

    def __init__(self) -> None:
        self._out = {}
        self._schedules = {}
        self._periods = []
        self._queue = []
        self._template = []
        self._target = TARGET_USAGE

    @property
    def _price_unit(self) -> str:
        return self.config_entry.data.get(CONF_PRICE_UNIT, DEFAULT_PRICE_UNIT)

    def _saved(self, target: str) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(OPTION_KEY_FOR_TARGET[target], []))

    def _defaults(self) -> dict[str, Any]:
        return dict(self.config_entry.options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose what to edit."""
        menu = ["general", "structure"]
        if self.config_entry.data.get(CONF_EXPORT_ENTITY):
            menu.append("feed_in_structure")
        if self.config_entry.data.get(CONF_CL_ENTITY):
            menu.append("cl_structure")
        menu.append("extras")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit charges, GST, discount, time basis, holidays and the glitch limit."""
        options = self._defaults()
        if user_input is not None:
            return self.async_create_entry(data={**options, **user_input})
        has_cl = bool(self.config_entry.data.get(CONF_CL_ENTITY))
        fields = {
            **_charges_schema(options, self._price_unit, has_cl=has_cl),
            **_bill_schema(options, with_unit=False),
            vol.Required(
                CONF_MAX_POWER_KW,
                default=options.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW),
            ): _number("kW", minimum=1, maximum=1000),
        }
        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(fields),
            description_placeholders=_units(self._price_unit),
        )

    async def _async_schedule_done(self, target: str) -> ConfigFlowResult:
        return await self._async_finish()

    async def _async_finish(self) -> ConfigFlowResult:
        options = {
            **self.config_entry.options,
            **self._out,
            **{OPTION_KEY_FOR_TARGET[t]: p for t, p in self._schedules.items()},
        }
        return self.async_create_entry(data=options)
