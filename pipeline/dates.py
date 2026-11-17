"""Date helpers. Snapshots are keyed by the US trading day whose close they report."""
from __future__ import annotations

import calendar
import datetime as dt
import functools
from typing import Optional

DAY = dt.timedelta(days=1)


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso_ts(moment=None):
    """UTC timestamp like 2026-11-14T11:32:10Z."""
    moment = moment or utc_now()
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(text):
    # type: (str) -> dt.date
    return dt.datetime.strptime(text[:10], "%Y-%m-%d").date()


def last_completed_friday(today):
    # type: (dt.date) -> dt.date
    """The most recent Friday strictly before `today` (UTC).

    A trading week counts as complete from Saturday onward, so a run on a Friday
    still reports the previous week.
    """
    days_back = (today.weekday() - 4) % 7  # Monday=0 ... Friday=4
    if days_back == 0:
        days_back = 7
    return today - dt.timedelta(days=days_back)


# --------------------------------------------------------------------------
# US market calendar (New York time, NYSE holidays)
# --------------------------------------------------------------------------
def _nth_weekday(year, month, weekday, n):
    """n-th (1-based) weekday of a month; n=-1 for the last one. weekday: Monday=0."""
    if n > 0:
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = month_end(year, month)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def to_eastern(moment):
    # type: (dt.datetime) -> dt.datetime
    """UTC datetime -> naive New York local time (US daylight-saving rules since 2007)."""
    utc = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    year = utc.year
    # DST runs from 2:00 local on the second Sunday of March (07:00 UTC)
    # to 2:00 local on the first Sunday of November (06:00 UTC).
    start = dt.datetime.combine(_nth_weekday(year, 3, 6, 2), dt.time(7))
    end = dt.datetime.combine(_nth_weekday(year, 11, 6, 1), dt.time(6))
    return utc - dt.timedelta(hours=4 if start <= utc < end else 5)


def _easter(year):
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def _observed(day):
    """A holiday on Saturday is observed on Friday, on Sunday on Monday."""
    if day.weekday() == 5:
        return day - DAY
    if day.weekday() == 6:
        return day + DAY
    return day


@functools.lru_cache(maxsize=None)
def market_holidays(year):
    """NYSE full-day closures for a year (rules as published by NYSE; unscheduled closures are not known here)."""
    days = {
        _nth_weekday(year, 1, 0, 3),          # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),          # Washington's Birthday
        _easter(year) - 2 * DAY,              # Good Friday
        _nth_weekday(year, 5, 0, -1),         # Memorial Day
        _observed(dt.date(year, 7, 4)),       # Independence Day
        _nth_weekday(year, 9, 0, 1),          # Labor Day
        _nth_weekday(year, 11, 3, 4),         # Thanksgiving
        _observed(dt.date(year, 12, 25)),     # Christmas
    }
    if year >= 2022:
        days.add(_observed(dt.date(year, 6, 19)))   # Juneteenth
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != 5:               # NYSE does not close the Friday before when Jan 1 is a Saturday
        days.add(_observed(new_year))
    return frozenset(days)


def is_trading_day(day):
    # type: (dt.date) -> bool
    return day.weekday() < 5 and day not in market_holidays(day.year)


def previous_trading_day(day):
    day -= DAY
    while not is_trading_day(day):
        day -= DAY
    return day


def next_trading_day(day):
    day += DAY
    while not is_trading_day(day):
        day += DAY
    return day


# A session's figures are treated as final from this New York hour on the same day
# (the close is 16:00; this leaves time for closing prices and overnight feeds to settle).
SESSION_FINAL_HOUR = 18


def last_completed_session(moment=None):
    # type: (Optional[dt.datetime]) -> dt.date
    """The most recent trading day whose figures are final at `moment` (a UTC datetime).

    A run at 6:00 New York time on Tuesday reports Monday's close; a run on Monday
    evening after 18:00 reports Monday too; a run on Saturday reports Friday.
    """
    local = to_eastern(moment or utc_now())
    day = local.date()
    if is_trading_day(day) and local.hour >= SESSION_FINAL_HOUR:
        return day
    return previous_trading_day(day)


def week_monday(day):
    # type: (dt.date) -> dt.date
    return day - dt.timedelta(days=day.weekday())


def from_epoch(ts, offset_seconds=0):
    # type: (float, int) -> dt.date
    """Exchange-local calendar date for a Unix timestamp."""
    return (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=ts + offset_seconds)).date()


def to_epoch(day):
    # type: (dt.date) -> int
    return int(calendar.timegm(day.timetuple()))


def month_end(year, month):
    # type: (int, int) -> dt.date
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def fiscal_label_year(period_end):
    # type: (dt.date) -> int
    """Calendar year a fiscal period is filed under.

    Periods are labelled by the year they end in. Subtracting two weeks keeps
    52/53-week fiscal years that spill a few days into January in the right year.
    """
    return (period_end - dt.timedelta(days=15)).year


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_month_year(text):
    # type: (str) -> Optional[dt.date]
    """'Oct 2026' -> date(2026, 10, 31)."""
    parts = (text or "").strip().split()
    if len(parts) != 2:
        return None
    month = _MONTHS.get(parts[0][:3].lower())
    try:
        year = int(parts[1])
    except ValueError:
        return None
    if not month or not 1990 <= year <= 2100:
        return None
    return month_end(year, month)


def years_between(start, end):
    # type: (dt.date, dt.date) -> float
    return (end - start).days / 365.25
