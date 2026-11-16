"""Date helpers. All 'weeks' are keyed by the Friday that closes them."""
from __future__ import annotations

import calendar
import datetime as dt
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
