"""Constants for AU Energy Tariff."""

from __future__ import annotations

DOMAIN = "au_energy_tariff"

# Config entry data (set once, at setup)
CONF_IMPORT_ENTITY = "import_entity"
CONF_EXPORT_ENTITY = "export_entity"
CONF_CL_ENTITY = "controlled_load_entity"
CONF_CURRENCY = "currency"
CONF_PRICE_UNIT = "price_unit"  # "cents" or "dollars", as printed on the bill

# Config entry options (editable afterwards)
CONF_PERIODS = "periods"  # usage rate windows
CONF_FEED_IN_PERIODS = "feed_in_periods"  # feed-in rate windows
CONF_CL_PERIODS = "controlled_load_periods"  # controlled load rate windows
CONF_SUPPLY_CHARGE = "supply_charge"  # per day, in the price unit
CONF_EXTRA_DAILY = "extra_daily"  # other fixed daily amount (negative = credit)
CONF_CL_SUPPLY_CHARGE = "controlled_load_supply_charge"
CONF_RATES_INCLUDE_GST = "rates_include_gst"
CONF_DISCOUNT_PERCENT = "discount_percent"
CONF_DISCOUNT_SCOPE = "discount_scope"  # "usage" or "usage_and_supply"
CONF_WINDOW_TIME = "window_time"  # "local" or "standard"
CONF_HOLIDAY_STATE = "holiday_state"  # "none" or a state/territory code
CONF_HOLIDAYS_AS = "holidays_as"  # "normal" or "weekend"
CONF_STEP_RESET_DAYS = "step_reset_days"
CONF_STEP_ANCHOR = "step_anchor"
CONF_DEMAND_ENABLED = "demand_enabled"
CONF_DEMAND_RATE = "demand_rate"
CONF_DEMAND_BASIS = "demand_basis"  # "day" or "month"
CONF_DEMAND_START = "demand_start"
CONF_DEMAND_END = "demand_end"
CONF_DEMAND_DAYS = "demand_days"
CONF_MAX_POWER_KW = "max_power_kw"

# Flow-only fields
CONF_TEMPLATE = "template"
CONF_PERIOD_NAME = "name"
CONF_PERIOD_START = "start"
CONF_PERIOD_END = "end"
CONF_PERIOD_RATE = "rate"
CONF_PERIOD_STEP_KWH = "step_kwh"
CONF_PERIOD_RATE_AFTER = "rate_after"
CONF_PERIOD_DAYS = "days"
CONF_ADD_ANOTHER = "add_another"
CONF_START_OVER = "start_over"

DEFAULT_NAME = "Electricity tariff"
DEFAULT_CURRENCY = "AUD"
DEFAULT_PRICE_UNIT = "cents"
DEFAULT_MAX_POWER_KW = 50.0

HOLIDAY_STATES = ["none", "ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"]

# Schedules the wizard can collect. Each has its own template list.
TARGET_USAGE = "usage"
TARGET_FEED_IN = "feed_in"
TARGET_CL = "controlled_load"
OPTION_KEY_FOR_TARGET = {
    TARGET_USAGE: CONF_PERIODS,
    TARGET_FEED_IN: CONF_FEED_IN_PERIODS,
    TARGET_CL: CONF_CL_PERIODS,
}
