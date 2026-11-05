# AU Energy Tariff

A Home Assistant integration that turns your **Australian electricity bill**
(time-of-use usage rates, daily supply charge, solar feed-in, controlled load
and demand charges) into cumulative cost sensors for the **Energy dashboard**.

Home Assistant's Energy dashboard has no way to include a fixed daily supply
charge ([home-assistant/core#80306](https://github.com/home-assistant/core/issues/80306)).
But a grid source accepts a cumulative monetary statistic as its *total cost*
(`stat_cost`) and its *total compensation* (`stat_compensation`). This
integration publishes exactly those sensors, so the supply charge and your
time-of-use costs show up natively in the Energy tab's totals and cost graphs.

It is built to fit the wide range of Australian plans, not one retailer: a
guided setup follows the layout of a bill, and anything that does not apply to
you is skipped.

## What it can price

| On your bill | Supported |
| --- | --- |
| Flat, two-rate, peak/shoulder/off-peak and "solar soak" usage rates | Yes, as time windows |
| Free or $0 midday power | Yes (a rate of `0`) |
| Step / block rates ("first 11 kWh a day at one price") | Yes, as an allowance |
| Free or discounted usage capped per day | Yes, as an allowance |
| Daily supply charge, other fixed daily fees or credits | Yes |
| GST-inclusive or GST-exclusive prices; cents or dollars | Yes |
| Guaranteed discount (usage only, or usage and supply) | Yes |
| Solar feed-in: flat, tiered (higher for the first kWh a day), time-varying, even negative | Yes |
| Controlled load on its own meter register, with its own supply charge | Yes |
| Demand charge (highest half hour in a peak window, per day or per month) | Yes |
| Weekday-only or weekend-only rates | Yes |
| Public holidays treated like a weekend (per state or territory) | Yes |
| Windows in standard time (Tasmania, ACT, some plans) | Yes |

See *Limitations* for what is not covered.

## Sensors

| Sensor | Use |
| --- | --- |
| **Import cost** (monetary) | Grid import priced at the rates in force at the time, plus the daily supply charge and any demand charge. Use as the grid import's *total costs* entity. |
| **Export credit** (monetary) | Export priced at your feed-in rates. Use as the grid export's *total compensation* entity. |
| **Controlled load cost** (monetary) | Controlled load usage and its own supply charge. Add it as a second grid source with its own cost entity. |
| Current tariff, Current import price, Current feed-in price | What applies right now. |
| Supply charge today | Today's fixed daily charge. |

The cost sensors expose a breakdown as attributes (`energy`, `supply_charge`,
`demand_charge`, `peak_demand_kw`) so you can reconcile them against a bill.

**Supply charge:** it is a fixed amount per day, so it is added once for each
local day, at midnight. If Home Assistant was off, the missed days are added
when it starts. Daily and monthly totals are exact; the hourly cost graph shows
the charge as one step at midnight. Installing mid-afternoon charges that
day's supply charge immediately, as a bill would.

> Home Assistant only allows `total` (not `total_increasing`) as the state
> class for monetary sensors. Cost sensors therefore use `total`.

## Requirements

* Home Assistant 2026.11 or newer.
* A cumulative energy sensor for grid **import**, and optionally **export**
  and a **controlled load** register, from your smart meter, solar inverter or
  an energy monitor. Wh, kWh and MWh are supported. Check that each measures
  energy at the **grid** side, not whole-house consumption.

## Install

HACS: add this repository as a custom repository (category *Integration*),
install, restart. Or copy `custom_components/au_energy_tariff` into your
config's `custom_components` folder and restart.

Then **Settings → Devices & services → Add integration → AU Energy Tariff**.

## Setup: a wizard that follows your bill

Have your bill open. The setup asks, in order (steps that do not apply are
skipped):

1. **Which sensors?** Grid import, and optionally export and controlled load.
2. **How does your bill print prices?** In **cents** or **dollars**; whether
   prices **include GST**; any **discount** and what it applies to; whether
   time windows follow the **clock on the wall** or **standard time**; and your
   state if public holidays change your rates.
3. **Daily charges.** Supply charge, any other fixed daily fee or credit, and
   the controlled load supply charge.
4. **Usage rates.** Pick the closest typical structure; windows are filled in
   for you and you just type the prices. Or choose *Custom*.
5. **Feed-in** (if you export). A flat rate, a higher rate for the first kWh
   each day, or different rates at different times.
6. **Controlled load rates** (if you have it).
7. **Extras.** A demand charge, and how often allowances reset.

| On your bill | In the wizard |
| --- | --- |
| "Supply charge", "Service to property" (per day) | Daily supply charge |
| "Solar feed-in", "Solar FiT", "Retailer feed-in tariff" (per kWh) | Feed-in rates |
| "Peak", "Off-peak", "Shoulder", "Super off-peak", "Solar soak" usage lines | One rate each, with the time window the bill lists |
| "First 11 kWh per day", "Step 1 / Step 2" | An allowance on that rate |
| "Rate incl. GST" column | Leave *Prices include GST* ticked |
| "Guaranteed" discount | Discount percentage (and what it applies to) |
| Free or $0 usage line | A rate of `0` (add an allowance if capped) |
| "Controlled load" usage and supply lines | Controlled load sensor, rates and supply charge |
| "Demand charge" in $/kW/day or $/kW/month | The demand step under *Extras* |

Windows are **local clock time**. An end time earlier than the start runs past
midnight (`21:00` to `11:00`), and the same start and end means all day.
Together the windows must cover every minute of every day exactly once; the
form tells you if there is a gap or an overlap. Each rate can be limited to
certain days, so a "weekday / weekend" plan is just two all-day rates.

You can change everything later from the integration's **Configure** screen.

### Allowances: step rates, capped free power and tiered feed-in

Any rate can have an optional **allowance**: the first *X* kWh per day are
charged the rate, and anything beyond is charged the *price after the
allowance*. The allowance is counted separately for each named window (windows
with the same name, such as two "Shoulder" windows, share one allowance) and
resets at midnight. This one idea covers:

* **Step / block rates:** one all-day rate with an allowance (first 11 kWh at
  30c, then 36c).
* **Capped free power:** a free window (`0`) with a daily allowance, then a
  normal price after the cap.
* **Tiered feed-in:** a higher feed-in rate for the first kWh exported each
  day, then the standard rate. Retailers usually average the cap over the
  billing period; this uses a daily allowance, which is very close.

If your allowance covers a longer period (for example per quarter), set
*Allowances reset every* to your billing period length under *Extras*.

### Daylight saving

Windows are evaluated against Home Assistant's own clock when each reading
arrives, so the meter's internal clock (smart meters record in AEST all year)
does not matter. What matters is how your plan defines its windows:

* **The clock on the wall** (the default): a 4pm peak is 4pm all year. This is
  how Victorian, NSW and SA plans normally work.
* **Standard time all year:** the windows do not move when daylight saving
  starts, so a "4pm" peak is 5pm on the wall clock from October to April. Some
  plans (for example in Tasmania and the ACT) work this way. Your plan's fact
  sheet will say; if in doubt, check the peak start time on a summer bill.

Queensland, WA and the NT don't observe daylight saving, so the two choices
are identical there.

### Public holidays

Some plans have no peak rates on public holidays, treating them like a
weekend; others treat them as normal days. Choose your state or territory and
*Weekend (Sunday) rates* to follow the first kind. Holidays come from the
`holidays` library that Home Assistant already uses (state-wide holidays;
local or regional holidays are not included).

### Demand charges

A demand charge is based on your highest 30 minute average demand (kW) inside
a peak window during a calendar month, multiplied by a price per kW per day or
per month. Enter the price, whether it is charged per day or per month, and
the window. The cost sensor adds each day's charge on the month's highest
demand so far, and tops up the days already charged when a new highest peak
is set, so by month end it equals *price × highest kW × days*. The current
highest demand is shown in the `peak_demand_kw` attribute.

### Example plans

All numbers below are invented for illustration. They are not real prices.

**A. Time of use with a midday solar-soak rate** (cents, GST-inclusive)

| Rate | From | To | Price |
| --- | --- | --- | --- |
| Peak | 16:00 | 21:00 | 46.200 c/kWh |
| Off-peak | 21:00 | 11:00 | 23.800 c/kWh |
| Solar soak | 11:00 | 16:00 | 8.500 c/kWh |

Supply charge 101.5 c/day. Choose *Peak, off-peak and a midday solar-soak
rate*. For feed-in choose *One flat rate* at 4.0 c/kWh.

**B. Peak, shoulder and off-peak, priced in dollars, with a discount**

| Rate | From | To | Price |
| --- | --- | --- | --- |
| Peak | 14:00 | 20:00 | $0.5120/kWh |
| Shoulder | 07:00 | 14:00 | $0.3040/kWh |
| Shoulder | 20:00 | 22:00 | $0.3040/kWh |
| Off-peak | 22:00 | 07:00 | $0.2150/kWh |

Choose *Dollars*, supply charge $1.0900/day and an 8% discount on usage only.

**C. Flat rate with cheaper weekends**

Choose *Custom* and add two rates, each 00:00 to 00:00: "Weekday" on Monday to
Friday at 31.0 c/kWh and "Weekend" on Saturday and Sunday at 24.5 c/kWh. Supply
charge 88.0 c/day.

**D. Four rates with a capped free midday window** (cents)

| Rate | From | To | Price |
| --- | --- | --- | --- |
| Peak | 16:00 | 21:00 | 39.000 c/kWh |
| Off-peak | 21:00 | 11:00 | 19.500 c/kWh |
| Free midday | 11:00 | 14:00 | 0 c/kWh, allowance 20 kWh, then 27.25 c/kWh |
| Shoulder | 14:00 | 16:00 | 27.250 c/kWh |

Supply charge 112.0 c/day. Choose *Four rates including a free midday window*
and give the free window an allowance of 20 kWh with a price after of 27.25.

**E. Step rates with a tiered solar feed-in**

Usage: choose *Step rates* with one all-day rate of 28.0 c/kWh, allowance
11 kWh, 34.5 c/kWh after. Feed-in: *Higher rate for the first kWh each day*
with 9.0 c/kWh, allowance 8 kWh, 3.0 c/kWh after.

**F. Controlled load and a demand charge**

Pick a controlled load sensor in the first step, enter its daily supply charge
(for example 1.1 c/day) and a single price (for example 17.0 c/kWh). Under
*Extras* tick the demand charge: 21.0 c/kW/day charged per day, window
16:00 to 21:00 every day.

## Wire it into the Energy dashboard

**Settings → Dashboards → Energy → Electricity grid**: edit your grid source,
choose *Use an entity tracking the total costs* and select **Import cost**;
for the return to grid choose *Use an entity tracking the total compensation*
and select **Export credit**. For controlled load add another grid source with
**Controlled load cost** as its cost entity.

New sensors record from the moment they are installed; earlier history is not
rewritten.

## How it stays correct

* Each positive step in a source sensor is priced at the rates in force across
  the time since the previous reading, so a restart or outage that spans a
  rate boundary or midnight is apportioned by time rather than billed at one
  rate.
* `unavailable`, `unknown` and non-numeric states are ignored; the baseline is
  kept, so nothing is lost or double counted when the source returns.
* A counter that goes **backwards** is treated as a reset: the baseline moves
  and nothing is billed (never a negative amount). Dips of 0.1 kWh or less are
  treated as noise.
* A step that implies more than the configured power (default 50 kW) is
  treated as a glitch: skipped and logged.
* Totals, the meter baseline, the allowance used so far, demand state and the
  last charged day are restored across restarts.

## Limitations

If your plan has one of these, the cost will be an approximation:

* **Conditional credits and bonuses** (bonus credits, interest rewards,
  daily credits that depend on how you use power, rebates and concessions) are
  not modelled. A fixed daily credit can be entered as a negative daily amount.
* **Demand charges** use calendar months, a single window and one maximum per
  month. Seasonal windows and averaging of several peaks are not supported.
* **Public holidays** are state-wide only (no local or regional days).
* **Tiered feed-in and allowances** reset daily (or every N days) rather than
  following your exact billing dates.
* **Price changes** are not dated: change the rates from the integration's
  options and only readings from then on use the new prices.
* No backfill of past months yet (planned as a separate step using recorder
  statistics import).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

The tariff maths in `custom_components/au_energy_tariff/tariff.py` has no Home
Assistant dependency. Tests use invented rates only.

## Licence

MIT
