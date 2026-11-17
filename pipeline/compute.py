"""Pure calculations: no network, no files. Everything here is unit-tested."""
from __future__ import annotations

import bisect
import datetime as dt
import math
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

DatedValue = Tuple[dt.date, float]


# --------------------------------------------------------------------------
# small numeric helpers
# --------------------------------------------------------------------------
def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def round_sig(x, sig=6):
    """Round to significant digits (keeps the JSON small without losing meaning)."""
    if not is_num(x):
        return None
    if x == 0:
        return 0
    digits = sig - int(math.floor(math.log10(abs(x)))) - 1
    value = round(x, digits)
    return int(value) if digits <= 0 else value


def safe_div(a, b):
    if not is_num(a) or not is_num(b) or b == 0:
        return None
    return a / b


def cagr(start_value, end_value, years):
    if not is_num(start_value) or not is_num(end_value) or not is_num(years):
        return None
    if start_value <= 0 or end_value <= 0 or years <= 0:
        return None
    return (end_value / start_value) ** (1.0 / years) - 1.0


# --------------------------------------------------------------------------
# time series lookups
# --------------------------------------------------------------------------
def value_on_or_before(points, day, max_age_days=None):
    # type: (Sequence[DatedValue], dt.date, Optional[int]) -> Optional[DatedValue]
    """Last (date, value) with date <= day. `points` must be sorted by date."""
    idx = bisect.bisect_right([p[0] for p in points], day) - 1
    if idx < 0:
        return None
    found = points[idx]
    if max_age_days is not None and (day - found[0]).days > max_age_days:
        return None
    return found


def index_cagrs(points, as_of, horizons):
    # type: (Sequence[DatedValue], dt.date, Sequence[int]) -> Dict[str, float]
    """CAGR of an index over each horizon (in years) ending at `as_of`."""
    out = {}
    end = value_on_or_before(points, as_of, max_age_days=10)
    if not end:
        return out
    for years in horizons:
        target = _years_ago(end[0], years)
        start = value_on_or_before(points, target, max_age_days=10)
        if not start:
            continue
        span = (end[0] - start[0]).days / 365.25
        rate = cagr(start[1], end[1], span)
        if rate is not None:
            out[str(years)] = round(rate, 6)
    return out


def _years_ago(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 Feb
        return day.replace(year=day.year - years, day=28)


# --------------------------------------------------------------------------
# currency conversion
# --------------------------------------------------------------------------
class FxBook(object):
    """Exchange rates into one base currency (the currency prices trade in), daily or weekly."""

    def __init__(self, base="USD"):
        self.base = base
        self._series = {}  # type: Dict[str, List[DatedValue]]

    def add(self, currency, points):
        self._series[currency] = sorted(points)

    def has(self, currency):
        return currency == self.base or currency in self._series

    def spot(self, currency, day):
        """Rate on `day` (latest close at or before it, at most 30 days old)."""
        if currency == self.base:
            return 1.0
        found = value_on_or_before(self._series.get(currency, ()), day, max_age_days=30)
        return found[1] if found else None

    def average(self, currency, start, end):
        """Mean rate over (start, end]. Needs observations covering most of the window."""
        if currency == self.base:
            return 1.0
        values = [v for d, v in self._series.get(currency, ()) if start < d <= end]
        expected = max(1.0, (end - start).days / 7.0)
        if len(values) < 0.6 * expected:
            return None
        return sum(values) / len(values)


# --------------------------------------------------------------------------
# dividends
# --------------------------------------------------------------------------
def dividend_metrics(payments, as_of, price):
    # type: (Sequence[DatedValue], dt.date, Optional[float]) -> Dict[str, object]
    """Trailing dividend, yield and 5-year growth from ex-dividend history.

    `payments` are (ex-date, amount per share), sorted, split-adjusted, on or before as_of.

    The trailing figure is anchored on the most recent payment rather than on a strict
    365-day window: companies that pay once a year move their date by a few weeks, and a
    strict window then counts zero or two payments.
    """
    out = {"ttm": 0.0, "freq": None, "yield": None, "growth5": None, "growth_stale": False, "last": None,
           "status": "none", "check": False}
    # status: paying | lapsed | gap (yield unknown) | none (yield 0)
    pays = [(d, a) for d, a in payments if d <= as_of and is_num(a) and a > 0]
    if not pays:
        if is_num(price) and price > 0:
            out["yield"] = 0.0
        return out

    last_date = pays[-1][0]
    gap = _typical_gap(pays)
    window = dt.timedelta(days=365 - gap / 2.0)
    out["last"] = last_date.isoformat()
    out["freq"] = max(1, min(12, int(round(365.0 / gap))))

    age = (as_of - last_date).days
    lapsed = age > gap + max(75.0, 0.25 * gap)
    if lapsed:
        # No payment when one was due: count strictly what was paid in the last 12 months.
        ttm = sum(a for d, a in pays if d > as_of - dt.timedelta(days=365))
        if ttm > 0:
            out["status"] = "lapsed"      # still some payments in the window, but the rhythm broke
        elif age <= 3 * 366:
            out["status"] = "gap"         # a regular payer with nothing on record lately: cut, or missing source data
        else:
            out["status"] = "none"        # stopped paying years ago
    else:
        ttm = sum(a for d, a in pays if d > last_date - window)
        out["status"] = "paying"
    out["ttm"] = round(ttm, 6)
    if is_num(price) and price > 0 and out["status"] != "gap":
        out["yield"] = round(ttm / price, 6)

    # Plausibility: a total wildly different from the year before usually means a one-off
    # special payment or holes in the source's history, so the caller can mark it for checking.
    if not lapsed:
        prior = sum(a for d, a in pays if last_date - window - dt.timedelta(days=365) < d <= last_date - window)
        if prior > 0 and (ttm < 0.25 * prior or ttm > 4 * prior):
            out["check"] = True
    if out["yield"] is not None and out["yield"] > 0.25:
        out["check"] = True

    # Growth is measured between the latest year of payments and the year five years before it.
    # When the source's history simply stops ("gap"), the same sum is still worth showing as long
    # as the caller says how old it is.
    latest_year = ttm if not lapsed else sum(a for d, a in pays if d > last_date - window)
    if latest_year > 0 and (not lapsed or out["status"] == "gap"):
        ttm_for_growth = latest_year
        out["growth_stale"] = lapsed
        target = last_date - dt.timedelta(days=1826)
        half_gap = dt.timedelta(days=gap / 2.0)
        earlier = [d for d, _ in pays if d <= target + half_gap]
        if earlier:
            anchor = earlier[-1]
            if abs((anchor - target).days) <= gap / 2.0 + 45:
                base = sum(a for d, a in pays if anchor - window < d <= anchor)
                rate = cagr(base, ttm_for_growth, (last_date - anchor).days / 365.25)
                # A history that already has holes is only trusted if both years hold the same number of payments.
                complete = not lapsed or (sum(1 for d, _ in pays if anchor - window < d <= anchor) ==
                                          sum(1 for d, _ in pays if d > last_date - window))
                if rate is not None and -0.95 < rate < 10 and complete:
                    out["growth5"] = round(rate, 6)
    return out


def _typical_gap(pays):
    """Median days between recent payments, clipped to [28, 365]."""
    dates = [d for d, _ in pays]
    cutoff = dates[-1] - dt.timedelta(days=3 * 366)
    first_recent = next(i for i, d in enumerate(dates) if d > cutoff)
    dates = dates[max(0, first_recent - 1):]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 5]
    if not gaps:
        return 365.0
    return float(max(28, min(365, median(gaps))))


# --------------------------------------------------------------------------
# Altman Z-Score (original 1968 model for public companies)
# --------------------------------------------------------------------------
Z_LABELS = ("working capital", "total assets", "retained earnings", "EBIT",
            "market value of equity", "total liabilities", "revenue")


def altman_z(working_capital, total_assets, retained_earnings, ebit, market_equity, total_liabilities, revenue):
    """Returns (z, components, missing). All money inputs must be in one currency.

    Z = 1.2*WC/TA + 1.4*RE/TA + 3.3*EBIT/TA + 0.6*MVE/TL + 1.0*Sales/TA
    """
    inputs = (working_capital, total_assets, retained_earnings, ebit, market_equity, total_liabilities, revenue)
    missing = [label for label, value in zip(Z_LABELS, inputs) if not is_num(value)]
    if missing:
        return None, None, missing
    if total_assets <= 0 or total_liabilities <= 0:
        return None, None, ["total assets and liabilities must be positive"]
    x = (working_capital / total_assets,
         retained_earnings / total_assets,
         ebit / total_assets,
         market_equity / total_liabilities,
         revenue / total_assets)
    z = 1.2 * x[0] + 1.4 * x[1] + 3.3 * x[2] + 0.6 * x[3] + 1.0 * x[4]
    return round(z, 3), [round(v, 4) for v in x], []


def z_zone(z):
    if not is_num(z):
        return None
    if z > 2.99:
        return "safe"
    if z >= 1.81:
        return "grey"
    return "distress"


# --------------------------------------------------------------------------
# Piotroski F-Score (nine yes/no tests on the last fiscal year vs the one before)
# --------------------------------------------------------------------------
F_TESTS = (
    "Positive net income",
    "Positive operating cash flow",
    "Return on assets improved",
    "Cash flow exceeds net income",
    "Long-term debt ratio fell",
    "Current ratio improved",
    "No new shares issued",
    "Gross margin improved",
    "Asset turnover improved",
)


def piotroski_f(years):
    # type: (Sequence[Dict[str, Optional[float]]]) -> Tuple[Optional[int], List[Optional[int]]]
    """`years` = [two years ago, last year, latest year]; each a dict with keys
    ni, cfo, ta, ltd, ca, cl, shares, gp, rev (any may be None).

    Returns (score, tests). `tests` has nine entries of 1, 0 or None (not computable).
    The score is only given when all nine tests could be computed.
    """
    if len(years) < 3:
        return None, [None] * 9
    y2, y1, y0 = years[-3], years[-2], years[-1]

    def gt(a, b):
        if not is_num(a) or not is_num(b):
            return None
        return 1 if a > b else 0

    roa0 = safe_div(y0.get("ni"), y1.get("ta"))          # on beginning-of-year assets
    roa1 = safe_div(y1.get("ni"), y2.get("ta"))
    avg_ta0 = _mean(y0.get("ta"), y1.get("ta"))
    avg_ta1 = _mean(y1.get("ta"), y2.get("ta"))
    lev0 = safe_div(y0.get("ltd"), avg_ta0)
    lev1 = safe_div(y1.get("ltd"), avg_ta1)
    cr0 = safe_div(y0.get("ca"), y0.get("cl"))
    cr1 = safe_div(y1.get("ca"), y1.get("cl"))
    gm0 = safe_div(y0.get("gp"), y0.get("rev"))
    gm1 = safe_div(y1.get("gp"), y1.get("rev"))
    at0 = safe_div(y0.get("rev"), y1.get("ta"))
    at1 = safe_div(y1.get("rev"), y2.get("ta"))

    if lev0 is None or lev1 is None:
        leverage = None
    else:
        leverage = 1 if (lev0 < lev1 or (lev0 == 0 and lev1 == 0)) else 0
    if not is_num(y0.get("shares")) or not is_num(y1.get("shares")):
        dilution = None
    else:
        dilution = 1 if y0["shares"] <= y1["shares"] * 1.0001 else 0

    tests = [
        gt(y0.get("ni"), 0),
        gt(y0.get("cfo"), 0),
        gt(roa0, roa1),
        gt(y0.get("cfo"), y0.get("ni")),
        leverage,
        gt(cr0, cr1),
        dilution,
        gt(gm0, gm1),
        gt(at0, at1),
    ]
    score = sum(tests) if all(t is not None for t in tests) else None
    return score, tests


def _mean(a, b):
    if not is_num(a) or not is_num(b):
        return None
    return (a + b) / 2.0
