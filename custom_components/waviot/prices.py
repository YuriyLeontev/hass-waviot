"""Tariff schedules for the cost statistics.

The "prices" option is a single text field, so it doubles as the place
where a tariff change is announced ahead of time:

    t1=6.97,t2=3.86                 one price per channel
    t1=6.97,t1@2026-10-01=7.86      t1 becomes 7.86 on 1 October 2026

Every hour of consumption is priced with the tariff in effect at that
hour, which is what makes a mid-year price rise come out right.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone

from homeassistant.util import dt as dt_util

from .const import PRICE_DATE_FORMATS, PRICE_KEY_ALIASES

# (valid from, price) pairs sorted by date. A start of None means "since
# forever" - the price before any dated change.
type PriceSchedule = list[tuple[datetime | None, float]]

# Sort key for an undated entry.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def parse_price_date(raw: str) -> date | None:
    """Parse the date of a price entry: 2026-10-01 or 01.10.2026."""
    for fmt in PRICE_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_prices(raw: str) -> tuple[dict[str, PriceSchedule], list[str]]:
    """Parse the prices option into a tariff schedule per channel.

    A key is a shortcut (t1..t4, tsum, total) or a full channel name. The
    date after "@" is local and means "from 00:00 of that day". An entry
    without a date is the starting price, and it also covers everything
    before the first dated change. Decimal comma is accepted ("6,97")
    when pairs are separated by ";".

    Returns the schedules plus the entries that could not be parsed, so
    the caller can either warn about them or reject the input.
    """
    raw = str(raw or "").strip()
    if not raw:
        return {}, []

    sep = ";" if ";" in raw else ","
    schedules: dict[str, PriceSchedule] = {}
    bad: list[str] = []

    for pair in raw.split(sep):
        if not pair.strip():
            continue
        if "=" not in pair:
            bad.append(pair.strip())
            continue

        key, value = pair.split("=", 1)
        key, _, day_raw = key.strip().lower().partition("@")

        start: datetime | None = None
        if day_raw:
            day = parse_price_date(day_raw.strip())
            if day is None:
                bad.append(pair.strip())
                continue
            start = dt_util.start_of_local_day(day)

        key = key.strip()
        key = PRICE_KEY_ALIASES.get(key, key)
        try:
            price = float(value.strip().replace(",", "."))
        except ValueError:
            bad.append(pair.strip())
            continue
        if not key:
            bad.append(pair.strip())
            continue
        schedules.setdefault(key, []).append((start, price))

    for entries in schedules.values():
        entries.sort(key=lambda item: item[0] or _EPOCH)
    return schedules, bad


def price_at(schedule: PriceSchedule, moment: datetime) -> float:
    """Price in effect at `moment` (the earliest one before that)."""
    price = schedule[0][1]
    for start, value in schedule:
        if start is not None and start > moment:
            break
        price = value
    return price


def cost_series(
    energy_rows: Iterable[tuple[datetime, float | None, float | None]],
    schedule: PriceSchedule,
) -> list[tuple[datetime, float | None, float]]:
    """Price an hourly energy series, hour by hour.

    Takes (hour, meter reading, cumulative kWh) rows in chronological
    order - the shape of the kWh statistics - and returns the same hours
    with a cumulative cost instead. Each hour is charged at the tariff in
    effect then, which is what lets a price change apply from its date
    without touching what came before it.
    """
    out: list[tuple[datetime, float | None, float]] = []
    run_sum = 0.0
    previous: float | None = None

    for hour, state, total_kwh in energy_rows:
        if total_kwh is None:
            continue
        if previous is not None:
            # Negative deltas are meter resets, already flattened when the
            # kWh series was imported; guard anyway.
            run_sum += max(total_kwh - previous, 0.0) * price_at(schedule, hour)
        previous = total_kwh
        out.append((hour, state, run_sum))
    return out
