"""Pure tariff maths for AU Energy Tariff.

Nothing in here imports Home Assistant, so it is easy to unit test.

Model
-----
A *schedule* is a list of ``Period`` windows. The same machinery prices
everything on a bill:

* usage (import) rates,
* feed-in (export) rates, including tiered and time-varying feed-in,
* controlled-load rates.

Any window can carry an optional allowance: "the first X kWh per day are
charged ``rate``, the rest ``rate_after``". That one idea covers step/block
tariffs, capped free power (rate 0 up to a cap) and tiered feed-in.

Conventions
-----------
* Rates are stored the way the customer's bill prints them: cents
  (c/kWh, c/day) or dollars ($/kWh, $/day), chosen by ``rate_divisor``.
  Results are always returned in whole currency units (dollars).
* Windows are written in either local wall-clock time or standard time
  (no daylight saving shift), see ``standard_time_windows``.
* A window applies to the weekday of the clock time itself. Optionally a
  public holiday counts as a Sunday.
* ``start == end`` means "all day". ``end < start`` wraps past midnight.
* Every datetime handed to the maths is converted to UTC first. Python
  compares and subtracts aware datetimes that share a ``ZoneInfo`` using
  naive wall-clock arithmetic, which is wrong across DST changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

MINUTES_PER_DAY = 1440
ALL_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # Monday=0 ... Sunday=6
SUNDAY = 6
GST_RATE = 0.10

# A decrease smaller than this is treated as sensor noise, not a reset.
RESET_TOLERANCE_KWH = 0.1
# Never infer power from a window shorter than this (avoids divide-by-tiny).
MIN_ELAPSED_SECONDS = 60.0

# Demand is measured over 30 minute intervals aligned to :00 and :30.
DEMAND_INTERVAL_SECONDS = 1800
MAX_CATCH_UP_DAYS = 3660

# How a bill prints prices -> divisor that turns them into dollars.
PRICE_UNIT_DIVISORS: dict[str, float] = {"cents": 100.0, "dollars": 1.0}

# Typical rate structures, as (name, start, end). The customer only has to
# fill in the prices. These are generic windows, not any one retailer's plan.
TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    # -- usage -------------------------------------------------------------
    "flat": [("Flat rate", "00:00", "00:00")],
    "step": [("Step rate", "00:00", "00:00")],
    "peak_offpeak": [
        ("Peak", "07:00", "23:00"),
        ("Off-peak", "23:00", "07:00"),
    ],
    "peak_shoulder_offpeak": [
        ("Peak", "14:00", "20:00"),
        ("Shoulder", "07:00", "14:00"),
        ("Shoulder", "20:00", "22:00"),
        ("Off-peak", "22:00", "07:00"),
    ],
    "solar_soak": [
        ("Peak", "16:00", "21:00"),
        ("Off-peak", "21:00", "11:00"),
        ("Solar soak", "11:00", "16:00"),
    ],
    # Four rates with a free (or very cheap) midday window, as on newer
    # "free power" style plans. Enter 0 for a free window; add an allowance
    # if the free usage is capped.
    "free_midday": [
        ("Peak", "16:00", "21:00"),
        ("Off-peak", "21:00", "11:00"),
        ("Free midday", "11:00", "14:00"),
        ("Shoulder", "14:00", "16:00"),
    ],
    # -- feed-in -----------------------------------------------------------
    "feed_flat": [("Feed-in", "00:00", "00:00")],
    "feed_tiered": [("Feed-in", "00:00", "00:00")],
    "feed_time_varying": [
        ("Evening", "16:00", "21:00"),
        ("Overnight", "21:00", "11:00"),
        ("Midday", "11:00", "16:00"),
    ],
    # -- controlled load -----------------------------------------------------
    "cl_flat": [("Controlled load", "00:00", "00:00")],
    "custom": [],
}

# Which templates are offered for each schedule.
TARGET_TEMPLATES: dict[str, list[str]] = {
    "usage": [
        "flat",
        "step",
        "peak_offpeak",
        "peak_shoulder_offpeak",
        "solar_soak",
        "free_midday",
        "custom",
    ],
    "feed_in": ["feed_flat", "feed_tiered", "feed_time_varying", "custom"],
    "controlled_load": ["cl_flat", "custom"],
}

ENERGY_UNIT_FACTORS: dict[str, float] = {
    "Wh": 0.001,
    "kWh": 1.0,
    "MWh": 1000.0,
    "GWh": 1_000_000.0,
}


def to_kwh(value: float, unit: str | None) -> float | None:
    """Convert an energy reading to kWh, or None if the unit is unsupported."""
    factor = ENERGY_UNIT_FACTORS.get(unit or "")
    return None if factor is None else value * factor


def parse_minutes(value: str) -> int:
    """Parse 'HH:MM' or 'HH:MM:SS' into minutes since midnight (24:00 -> 0)."""
    parts = str(value).split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError) as err:
        raise ValueError(f"Invalid time: {value!r}") from err
    if not (0 <= hours <= 24 and 0 <= minutes < 60) or (hours == 24 and minutes):
        raise ValueError(f"Invalid time: {value!r}")
    return (hours * 60 + minutes) % MINUTES_PER_DAY


def format_minutes(minutes: int) -> str:
    """Format minutes since midnight as HH:MM."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _opt_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


@dataclass(frozen=True)
class Period:
    """One named rate window, optionally with a daily allowance."""

    name: str
    start: int  # minutes since local midnight
    end: int  # minutes since local midnight
    rate: float  # per kWh, in the bill's price unit
    days: tuple[int, ...] = ALL_DAYS
    # Allowance: the first ``step_kwh`` kWh per reset period (usually per day)
    # in this window are charged ``rate``, anything beyond ``rate_after``.
    step_kwh: float | None = None
    rate_after: float | None = None

    def covers(self, minute: int, weekday: int) -> bool:
        """Return True if this period is in force at the given local time."""
        if weekday not in self.days:
            return False
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Period:
        """Build a Period from its stored form."""
        return cls(
            name=str(data["name"]),
            start=parse_minutes(data["start"]),
            end=parse_minutes(data["end"]),
            rate=float(data["rate"]),
            days=tuple(sorted({int(d) for d in data.get("days", ALL_DAYS)})),
            step_kwh=_opt_float(data.get("step_kwh")),
            rate_after=_opt_float(data.get("rate_after")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the stored (JSON friendly) form."""
        out: dict[str, Any] = {
            "name": self.name,
            "start": format_minutes(self.start),
            "end": format_minutes(self.end),
            "rate": self.rate,
            "days": list(self.days),
        }
        if self.step_kwh is not None:
            out["step_kwh"] = self.step_kwh
            out["rate_after"] = self.rate_after
        return out


def validate_periods(periods: Sequence[Period]) -> str | None:
    """Check periods cover every minute of every weekday exactly once.

    Returns an error key ("no_periods", "gap", "overlap") or None if valid.
    """
    if not periods:
        return "no_periods"
    counts = [[0] * MINUTES_PER_DAY for _ in range(7)]
    for period in periods:
        for weekday in period.days:
            row = counts[weekday]
            for minute in range(MINUTES_PER_DAY):
                if period.covers(minute, weekday):
                    row[minute] += 1
    flat = [c for row in counts for c in row]
    if any(c == 0 for c in flat):
        return "gap"
    if any(c > 1 for c in flat):
        return "overlap"
    return None


def evaluate_reading(
    baseline_kwh: float | None,
    new_kwh: float,
    elapsed_seconds: float,
    max_power_kw: float,
) -> tuple[float, float | None, str]:
    """Decide what to do with a new cumulative reading.

    Returns ``(delta_kwh, new_baseline, reason)`` where reason is one of
    ``initial``, ``ok``, ``unchanged``, ``noise``, ``reset`` or ``jump``.
    Only ``ok`` returns a positive delta; every other outcome adds nothing,
    so a restart, reset or glitch can never double count or go negative.
    """
    if baseline_kwh is None:
        return 0.0, new_kwh, "initial"
    diff = new_kwh - baseline_kwh
    if diff == 0:
        return 0.0, baseline_kwh, "unchanged"
    if diff < 0:
        if -diff <= RESET_TOLERANCE_KWH:
            # Tiny dip: keep the old baseline so the recovery isn't re-billed.
            return 0.0, baseline_kwh, "noise"
        return 0.0, new_kwh, "reset"
    hours = max(elapsed_seconds, MIN_ELAPSED_SECONDS) / 3600
    if diff / hours > max_power_kw:
        return 0.0, new_kwh, "jump"
    return diff, new_kwh, "ok"


# ---------------------------------------------------------------------------
# Running state that the cost sensors persist
# ---------------------------------------------------------------------------


@dataclass
class BlockState:
    """How much of each window's allowance has been used in this reset period."""

    key: str | None = None
    used: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON friendly dict."""
        return {"key": self.key, "used": dict(self.used)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> BlockState:
        """Restore from ``to_dict`` output (tolerates missing data)."""
        if not data:
            return cls()
        return cls(
            key=data.get("key"),
            used={str(k): float(v) for k, v in (data.get("used") or {}).items()},
        )


@dataclass
class DemandState:
    """Running state for a demand charge."""

    month: str | None = None  # "YYYY-MM" of the month being tracked
    max_kw: float = 0.0  # highest 30 minute demand in the window this month
    days: int = 0  # days of this month already charged (per-day basis)
    interval: int | None = None  # open 30 minute interval (epoch // 1800)
    interval_kwh: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON friendly dict."""
        return {
            "month": self.month,
            "max_kw": self.max_kw,
            "days": self.days,
            "interval": self.interval,
            "interval_kwh": self.interval_kwh,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> DemandState:
        """Restore from ``to_dict`` output (tolerates missing data)."""
        if not data:
            return cls()
        interval = data.get("interval")
        return cls(
            month=data.get("month"),
            max_kw=float(data.get("max_kw", 0.0)),
            days=int(data.get("days", 0)),
            interval=None if interval is None else int(interval),
            interval_kwh=float(data.get("interval_kwh", 0.0)),
        )


@dataclass(frozen=True)
class DemandCharge:
    """A network-style demand charge.

    The highest 30 minute average demand (kW) inside ``window`` during a
    calendar month, multiplied by ``rate`` per kW per day (``basis="day"``,
    charged for every day of the month) or per kW per month
    (``basis="month"``).
    """

    rate: float  # per kW, in the bill's price unit
    basis: str  # "day" or "month"
    window: Period


# ---------------------------------------------------------------------------
# The tariff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tariff:
    """One priced schedule: rate windows plus the daily and demand extras."""

    periods: tuple[Period, ...]
    supply_charge: float  # per day, in the bill's price unit
    tz: tzinfo
    rates_include_gst: bool = True  # False: 10% GST is added
    rate_divisor: float = 100.0  # 100 = bill prints cents, 1 = prints dollars
    discount_percent: float = 0.0  # off usage (and optionally supply)
    discount_on_supply: bool = False  # most plans discount usage only
    # True: windows are in standard time all year (as for some networks and
    # retailers), so they shift on the wall clock during daylight saving.
    standard_time_windows: bool = False
    # Any other fixed daily amount (positive = charge, negative = credit).
    extra_daily: float = 0.0
    # Public holidays: a callable telling if a date is a holiday, and whether
    # such days take Sunday's rates.
    holiday_check: Callable[[date], bool] | None = field(default=None, compare=False)
    holidays_as_weekend: bool = False
    # Allowances reset every ``step_reset_days`` days (1 = daily), counted
    # from ``step_anchor`` when longer than a day.
    step_reset_days: int = 1
    step_anchor: date | None = None
    demand: DemandCharge | None = None

    # -- helpers ---------------------------------------------------------

    @property
    def _gst(self) -> float:
        return 1.0 if self.rates_include_gst else 1.0 + GST_RATE

    @property
    def _usage_factor(self) -> float:
        """Multiplier turning a billed usage price into dollars."""
        return self._gst * (1.0 - self.discount_percent / 100.0) / self.rate_divisor

    @property
    def _supply_factor(self) -> float:
        """Multiplier turning the billed supply charge into dollars."""
        discount = (
            1.0 - self.discount_percent / 100.0 if self.discount_on_supply else 1.0
        )
        return self._gst * discount / self.rate_divisor

    def _window_tz(self, at: datetime) -> tzinfo:
        """Time zone the rate windows are written in.

        Normally the local zone. With ``standard_time_windows`` it is the
        zone's fixed standard-time offset (no daylight saving shift).
        """
        if not self.standard_time_windows:
            return self.tz
        local = at.astimezone(self.tz)
        offset = local.utcoffset() or timedelta(0)
        return timezone(offset - (local.dst() or timedelta(0)))

    def _weekday(self, local: datetime) -> int:
        if (
            self.holidays_as_weekend
            and self.holiday_check is not None
            and self.holiday_check(local.date())
        ):
            return SUNDAY
        return local.weekday()

    def _boundary_minutes(self) -> set[int]:
        minutes = {0}
        for period in self.periods:
            minutes.add(period.start)
            minutes.add(period.end)
        return minutes

    def _reset_key(self, when: datetime) -> str:
        """Identifier of the allowance reset period containing ``when``."""
        day = when.astimezone(self.tz).date()
        if self.step_reset_days <= 1:
            return day.isoformat()
        anchor = self.step_anchor or date(2000, 1, 1)
        return f"c{(day - anchor).days // self.step_reset_days}"

    # -- lookups -----------------------------------------------------------

    def period_at(self, when: datetime) -> Period | None:
        """Return the period in force at ``when``."""
        local = when.astimezone(self._window_tz(when))
        minute = local.hour * 60 + local.minute
        weekday = self._weekday(local)
        for period in self.periods:
            if period.covers(minute, weekday):
                return period
        return None

    def price_at(self, when: datetime) -> float:
        """Price in dollars per kWh at ``when`` (before any allowance)."""
        period = self.period_at(when)
        return 0.0 if period is None else period.rate * self._usage_factor

    # -- energy cost ---------------------------------------------------------

    def _segments(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Split [start, end) at every rate boundary and local midnight."""
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        points = {start, end}
        minutes = self._boundary_minutes()

        wtz = self._window_tz(start)
        day = start.astimezone(wtz).date() - timedelta(days=1)
        last = end.astimezone(wtz).date() + timedelta(days=1)
        while day <= last:
            for minute in minutes:
                wall = datetime.combine(
                    day, time(minute // 60, minute % 60), tzinfo=wtz
                ).astimezone(UTC)
                if start < wall < end:
                    points.add(wall)
            day += timedelta(days=1)

        # Allowances and holidays reset at *local* midnight.
        day = start.astimezone(self.tz).date()
        last = end.astimezone(self.tz).date() + timedelta(days=1)
        while day <= last:
            midnight = datetime.combine(day, time.min, tzinfo=self.tz).astimezone(UTC)
            if start < midnight < end:
                points.add(midnight)
            day += timedelta(days=1)

        ordered = sorted(points)
        return list(zip(ordered, ordered[1:], strict=False))

    def energy_cost(
        self,
        start: datetime,
        end: datetime,
        kwh: float,
        state: BlockState | None = None,
    ) -> float:
        """Cost in dollars of ``kwh`` used evenly between start and end.

        If the interval crosses rate boundaries the energy is apportioned by
        time, so a long gap (restart, outage) is priced sensibly. A zero or
        negative-length interval is priced at the rate in force at ``end``.
        Allowances are tracked in ``state`` (mutated in place).
        """
        if kwh == 0:
            return 0.0
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if end <= start:
            pieces = [(end, kwh)]
        else:
            total = (end - start).total_seconds()
            pieces = [
                (seg_start, kwh * (seg_end - seg_start).total_seconds() / total)
                for seg_start, seg_end in self._segments(start, end)
            ]

        cost = 0.0
        factor = self._usage_factor
        for when, share in pieces:
            period = self.period_at(when)
            if period is None:
                continue
            if period.step_kwh is None or state is None or share < 0:
                cost += share * period.rate * factor
                continue
            key = self._reset_key(when)
            if state.key != key:
                state.key, state.used = key, {}
            used = state.used.get(period.name, 0.0)
            within = min(share, max(period.step_kwh - used, 0.0))
            after = period.rate if period.rate_after is None else period.rate_after
            cost += (within * period.rate + (share - within) * after) * factor
            state.used[period.name] = used + share
        return cost

    # -- fixed daily charges ---------------------------------------------------

    @property
    def daily_supply_cost(self) -> float:
        """The fixed daily charge for one local day, in dollars.

        The supply charge is a flat amount per day, so it is simply added once
        for each local calendar day (no pro-rating by hour). Any other fixed
        daily charge or credit is included.
        """
        return (self.supply_charge + self.extra_daily) * self._supply_factor

    # -- demand charge ---------------------------------------------------------

    @staticmethod
    def _month_key(when: datetime, tz: tzinfo) -> str:
        local = when.astimezone(tz)
        return f"{local.year}-{local.month:02d}"

    def _demand_unit(self) -> float:
        assert self.demand is not None
        return self.demand.rate * self._supply_factor

    def _demand_roll(self, state: DemandState, key: str) -> bool:
        """Move to a new month. Returns False if ``key`` is an older month."""
        if state.month is None or key > state.month:
            state.month, state.max_kw, state.days = key, 0.0, 0
        return key >= (state.month or key)

    def _demand_covers(self, when: datetime) -> bool:
        assert self.demand is not None
        local = when.astimezone(self._window_tz(when))
        return self.demand.window.covers(
            local.hour * 60 + local.minute, self._weekday(local)
        )

    def _close_interval(self, state: DemandState, interval: int, kwh: float) -> float:
        """Register a finished 30 minute interval; returns any extra cost."""
        if self.demand is None or kwh <= 0:
            return 0.0
        istart = datetime.fromtimestamp(interval * DEMAND_INTERVAL_SECONDS, UTC)
        if not self._demand_covers(istart):
            return 0.0
        if not self._demand_roll(state, self._month_key(istart, self.tz)):
            return 0.0  # a late interval from a previous month
        kw = kwh * 3600 / DEMAND_INTERVAL_SECONDS
        if kw <= state.max_kw:
            return 0.0
        delta, state.max_kw = kw - state.max_kw, kw
        unit = self._demand_unit()
        return delta * unit * (state.days if self.demand.basis == "day" else 1)

    def demand_register(
        self, state: DemandState, start: datetime, end: datetime, kwh: float
    ) -> float:
        """Feed energy into the 30 minute demand intervals.

        Returns any extra demand cost from intervals that closed and set a new
        monthly maximum.
        """
        if self.demand is None or kwh <= 0:
            return 0.0
        t0, t1 = start.timestamp(), end.timestamp()
        step = DEMAND_INTERVAL_SECONDS
        if t1 <= t0:
            pieces = [(int(t1 // step), kwh)]
        else:
            pieces = []
            idx = int(t0 // step)
            while idx * step < t1:
                lo, hi = max(t0, idx * step), min(t1, (idx + 1) * step)
                pieces.append((idx, kwh * (hi - lo) / (t1 - t0)))
                idx += 1
        cost = 0.0
        for idx, share in pieces:
            if state.interval is None:
                state.interval = idx
            if idx != state.interval:
                cost += self._close_interval(state, state.interval, state.interval_kwh)
                state.interval, state.interval_kwh = idx, 0.0
            state.interval_kwh += share
        return cost

    def demand_flush(self, state: DemandState, now: datetime) -> float:
        """Close the open interval once it has finished."""
        if self.demand is None or state.interval is None:
            return 0.0
        if int(now.timestamp() // DEMAND_INTERVAL_SECONDS) <= state.interval:
            return 0.0
        cost = self._close_interval(state, state.interval, state.interval_kwh)
        state.interval, state.interval_kwh = None, 0.0
        return cost

    def demand_daily(self, state: DemandState, day: date) -> float:
        """Add one day of a per-day demand charge (month rolls handled)."""
        if self.demand is None:
            return 0.0
        self._demand_roll(state, f"{day.year}-{day.month:02d}")
        state.days += 1
        if self.demand.basis != "day":
            return 0.0
        return self._demand_unit() * state.max_kw
