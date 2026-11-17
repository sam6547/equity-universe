"""Yahoo Finance: the three JSON endpoints that answer an ordinary script.

  chart                     prices (daily bars), name, exchange, dividend history
  fundamentals-timeseries   financial statements and valuation measures
  search                    sector and industry

Yahoo's richer "quoteSummary" data (profile country, analyst estimates, levered free
cash flow) sits behind a session token that Yahoo only issues to browsers, so it is
deliberately not used here.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import urllib.parse
from typing import Dict, List, Optional, Tuple

from .. import compute, dates, net
from ..result import Result

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?%s"
TS_URL = "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s?%s"
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search?%s"

Point = collections.namedtuple("Point", "date value currency")

_ANNUAL = ["TotalRevenue", "GrossProfit", "EBIT", "NetIncome", "NetIncomeCommonStockholders", "DilutedEPS", "BasicEPS",
           "OperatingCashFlow", "FreeCashFlow", "TotalAssets", "CurrentAssets", "CurrentLiabilities",
           "TotalLiabilitiesNetMinorityInterest", "RetainedEarnings", "LongTermDebt", "OrdinarySharesNumber",
           "DilutedAverageShares", "WorkingCapital", "EBITDA", "NormalizedEBITDA",
           "CashDividendsPaid", "StockholdersEquity", "CapitalStock", "AdditionalPaidInCapital", "TreasuryStock"]
_QUARTERLY = ["TotalAssets", "CurrentAssets", "CurrentLiabilities", "TotalLiabilitiesNetMinorityInterest",
              "RetainedEarnings", "WorkingCapital", "StockholdersEquity", "CapitalStock", "AdditionalPaidInCapital",
              "TreasuryStock"]
_TRAILING = ["TotalRevenue", "EBIT", "EBITDA", "NormalizedEBITDA", "FreeCashFlow", "DilutedEPS", "CashDividendsPaid",
             "MarketCap", "EnterpriseValue", "PeRatio", "ForwardPeRatio", "PegRatio"]
TS_TYPES = (["annual" + t for t in _ANNUAL] + ["quarterly" + t for t in _QUARTERLY] + ["trailing" + t for t in _TRAILING])

# Yahoo exchange codes -> the short label shown in the table.
_EXCHANGES = {
    "NYQ": "NYSE", "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "ASE": "NYSE American", "PCX": "NYSE Arca",
    "BTS": "Cboe", "TEX": "TXSE", "PNK": "OTC", "OQX": "OTC", "OQB": "OTC", "OID": "OTC", "OEM": "OTC", "OGM": "OTC",
}
LISTED_EXCHANGES = ("NYSE", "NASDAQ", "NYSE American", "NYSE Arca", "Cboe", "TXSE")

# Currencies quoted in 1/100 units.
_SUBUNITS = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ILA": ("ILS", 0.01), "ZAc": ("ZAR", 0.01)}


def yahoo_symbol(symbol):
    """Workbook 'BF/B' -> Yahoo 'BF-B'."""
    return symbol.strip().upper().replace("/", "-").replace(".", "-")


def split_currency(code):
    # type: (Optional[str]) -> Tuple[Optional[str], float]
    """('GBp') -> ('GBP', 0.01); ordinary codes pass through with scale 1."""
    if not code:
        return None, 1.0
    if code in _SUBUNITS:
        return _SUBUNITS[code]
    return code.upper(), 1.0


# --------------------------------------------------------------------------
# chart: prices and dividends
# --------------------------------------------------------------------------
def chart_url(symbol, as_of, years=6.3, interval="1d", events=True):
    """Daily bars reaching back far enough for 5-year dividend growth (5 years plus a year of payments)."""
    params = {
        "period1": dates.to_epoch(as_of - dt.timedelta(days=int(years * 365.25) + 14)),
        "period2": dates.to_epoch(as_of + dt.timedelta(days=2)),
        "interval": interval,
    }
    if events:
        params["events"] = "div,split"
    return CHART_URL % (urllib.parse.quote(symbol, safe=""), urllib.parse.urlencode(params))


def parse_chart(payload):
    """-> (meta, daily bars, dividends).

    bars: list of (date, close, high, low) sorted by date, one per trading day (exchange-local date).
    dividends: list of (ex_date, amount) sorted by date.
    Raises ValueError when the payload has no usable price series.
    """
    chart = (payload or {}).get("chart") or {}
    results = chart.get("result") or []
    if not results:
        err = chart.get("error") or {}
        raise ValueError(err.get("description") or "no chart data")
    res = results[0]
    meta = res.get("meta") or {}
    offset = int(meta.get("gmtoffset") or 0)
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    closes = quote.get("close") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []

    days = collections.OrderedDict()
    for i, ts in enumerate(stamps):
        close = closes[i] if i < len(closes) else None
        if not compute.is_num(close) or close <= 0:
            continue
        day = dates.from_epoch(ts, offset)
        high = highs[i] if i < len(highs) and compute.is_num(highs[i]) else close
        low = lows[i] if i < len(lows) and compute.is_num(lows[i]) else close
        prev = days.get(day)
        if prev:  # Yahoo can append a "live" point for the current day: merge it into that day's bar
            high, low = max(high, prev[2]), min(low, prev[3])
        days[day] = (day, float(close), float(high), float(low))
    bars = sorted(days.values())
    if not bars:
        raise ValueError("no prices in chart data")

    divs = []
    for item in ((res.get("events") or {}).get("dividends") or {}).values():
        amount, ts = item.get("amount"), item.get("date")
        if compute.is_num(amount) and amount > 0 and compute.is_num(ts):
            divs.append((dates.from_epoch(ts, offset), float(amount)))
    divs.sort()
    return meta, bars, divs


def quote_fields(meta, bars, divs, as_of):
    """Everything the dashboard shows from the chart endpoint, as of the close of `as_of`.

    Bars after `as_of` are ignored, so a retry that runs while the market is open still
    reports the as-of close, never an intraday price.
    """
    usable = [b for b in bars if b[0] <= as_of]
    if not usable:
        raise ValueError("no prices on or before %s" % as_of.isoformat())
    bar = usable[-1]
    price = bar[1]
    closes = [(b[0], b[1]) for b in usable]

    out = {
        "yname": meta.get("longName") or meta.get("shortName"),
        "exch": _exchange_label(meta),
        "exch_raw": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "cur": meta.get("currency"),
        "price": compute.round_sig(price, 7),
        "pdate": bar[0].isoformat(),
    }
    lag, day = 0, bar[0]
    while day < as_of and lag < 400:
        day = dates.next_trading_day(day)
        lag += 1
    if lag > 0:
        out["plag"] = lag  # no trade on the as-of day: the price is this many trading days old
    if len(usable) >= 2 and (bar[0] - usable[-2][0]).days <= 7:
        out["chg1d"] = round(price / usable[-2][1] - 1.0, 5)
    week = compute.value_on_or_before(closes, bar[0] - dt.timedelta(days=7), max_age_days=10)
    if week:
        out["chg1w"] = round(price / week[1] - 1.0, 5)
    year = [b for b in usable if b[0] > bar[0] - dt.timedelta(days=365)]
    if year:
        # include closes: for thinly traded tickers Yahoo sometimes reports a low above the close
        out["hi52"] = compute.round_sig(max(max(b[1], b[2]) for b in year), 7)
        out["lo52"] = compute.round_sig(min(min(b[1], b[3]) for b in year), 7)
    ago = compute.value_on_or_before(closes, bar[0] - dt.timedelta(days=365), max_age_days=10)
    if ago:
        out["ret1y"] = round(price / ago[1] - 1.0, 5)

    dm = compute.dividend_metrics(divs, as_of, price)
    out["dy"] = dm["yield"]
    out["div_ttm"] = compute.round_sig(dm["ttm"], 6)
    if dm["growth5"] is not None:
        out["dg5"] = dm["growth5"]
        if dm["growth_stale"]:
            out["dg5_old"] = 1  # measured up to the last payment on record, which is over a year ago
    if dm["freq"]:
        out["div_freq"] = dm["freq"]
    if dm["last"]:
        out["div_last"] = dm["last"]
    if dm["status"] == "lapsed":
        out["div_lapsed"] = 1   # payments slowed or stopped: yield covers only what was paid in the last 12 months
    if dm["check"]:
        out["div_odd"] = 1      # total looks implausible beside last year's: special payment or holes in the source
    if dm["status"] == "gap":
        out["div_gap"] = 1      # regular payer with nothing on record for over a year: yield left blank, not shown as 0
        out.pop("div_ttm", None)
    return {k: v for k, v in out.items() if v is not None}


def _exchange_label(meta):
    code, full = meta.get("exchangeName"), meta.get("fullExchangeName") or ""
    if code in _EXCHANGES:
        return _EXCHANGES[code]
    if full.upper().startswith("OTC"):
        return "OTC"            # any OTC Markets tier Yahoo adds later
    return full or code


def fetch_quote(client, symbol, as_of):
    # type: (net.Client, str, dt.date) -> Result
    resp = client.get(chart_url(yahoo_symbol(symbol), as_of))
    if resp.kind == net.NOT_FOUND:
        return Result.no_data("Yahoo has no price history for this symbol (renamed or delisted?)")
    if not resp.ok:
        return Result.failed(resp)
    try:
        meta, bars, divs = parse_chart(json.loads(resp.body.decode("utf-8", "replace")))
        return Result.ok(quote_fields(meta, bars, divs, as_of))
    except ValueError as exc:
        return Result.no_data("Yahoo price data unusable: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - unexpected layout: report as a source failure
        return Result.broken("unexpected chart format: %s" % type(exc).__name__)


def fetch_series(client, symbol, start, end, interval="1d"):
    # type: (net.Client, str, dt.date, dt.date, str) -> Tuple[Optional[List[Tuple[dt.date, float]]], str]
    """Daily closes for an index or currency pair, up to and including `end`. -> (points or None, error)."""
    params = urllib.parse.urlencode({"period1": dates.to_epoch(start), "period2": dates.to_epoch(end + dt.timedelta(days=2)),
                                     "interval": interval})
    resp = client.get(CHART_URL % (urllib.parse.quote(symbol, safe=""), params))
    if not resp.ok:
        return None, resp.describe()
    try:
        _meta, bars, _divs = parse_chart(json.loads(resp.body.decode("utf-8", "replace")))
    except Exception as exc:  # noqa: BLE001
        return None, "unusable chart data: %s" % exc
    return [(b[0], b[1]) for b in bars if b[0] <= end], ""


# --------------------------------------------------------------------------
# fundamentals-timeseries: statements and valuation
# --------------------------------------------------------------------------
def timeseries_url(symbol, as_of):
    params = {
        "type": ",".join(TS_TYPES),
        "period1": dates.to_epoch(as_of - dt.timedelta(days=6 * 366)),
        "period2": dates.to_epoch(as_of + dt.timedelta(days=12)),
    }
    return TS_URL % (urllib.parse.quote(symbol, safe=""), urllib.parse.urlencode(params, safe=","))


def parse_timeseries(payload):
    # type: (dict) -> Dict[str, List[Point]]
    """-> {series name: [Point, ...] sorted by date}. Raises ValueError on an unknown layout."""
    ts = (payload or {}).get("timeseries")
    if not isinstance(ts, dict) or not isinstance(ts.get("result"), list):
        raise ValueError("unexpected timeseries layout")
    out = {}
    for series in ts["result"]:
        names = (series.get("meta") or {}).get("type") or []
        if not names:
            continue
        name = names[0]
        points = []
        for item in series.get(name) or []:
            if not item:
                continue
            raw = (item.get("reportedValue") or {}).get("raw")
            day = item.get("asOfDate")
            if compute.is_num(raw) and day:
                points.append(Point(dates.parse_date(day), float(raw), item.get("currencyCode")))
        if points:
            out[name] = sorted(points)
    return out


def _near(points, day, tolerance=6):
    """Value of the point dated within `tolerance` days of `day`."""
    for p in points or ():
        if abs((p.date - day).days) <= tolerance:
            return p.value
    return None


def _latest(points, as_of, max_age_days, slack_days=12):
    """Most recent point not after as_of (+slack) and not older than max_age_days."""
    best = None
    for p in points or ():
        if p.date <= as_of + dt.timedelta(days=slack_days):
            best = p
    if best is not None and (as_of - best.date).days <= max_age_days:
        return best
    return None


def _closest(points, as_of, window_days=12):
    """Point closest to as_of within +/- window (ties go to the earlier date)."""
    cands = [p for p in points or () if abs((p.date - as_of).days) <= window_days]
    if not cands:
        return None
    return min(cands, key=lambda p: (abs((p.date - as_of).days), p.date))


def fiscal_year_ends(series):
    # type: (Dict[str, List[Point]]) -> List[dt.date]
    """Fiscal year-end dates, oldest first, ignoring stray interim rows Yahoo sometimes mixes in."""
    seen = set()
    for name in ("annualTotalAssets", "annualNetIncome", "annualTotalRevenue", "annualDilutedEPS"):
        for p in series.get(name, ()):
            seen.add(p.date)
        if len(seen) >= 3:
            break
    if not seen:
        return []
    ordered = sorted(seen)
    anchor = ordered[-1].timetuple().tm_yday

    def aligned(day):
        diff = abs(day.timetuple().tm_yday - anchor)
        return min(diff, 366 - diff) <= 20

    out = []
    for day in ordered:
        if aligned(day) and (not out or (day - out[-1]).days > 300):
            out.append(day)
    return out


def reporting_currency(series):
    for name in ("annualTotalRevenue", "annualNetIncome", "annualTotalAssets", "trailingTotalRevenue", "annualDilutedEPS"):
        pts = series.get(name)
        if pts and pts[-1].currency:
            return pts[-1].currency
    return None


def fundamentals_fields(series, as_of, price=None, trade_currency="USD", fx=None):
    """Derived fundamentals for one ticker.

    Statement values come in the company's reporting currency; per-share and money
    figures are converted into the currency the stock trades in:
      * fiscal-year figures at that year's average rate
      * trailing-twelve-month figures at the trailing year's average rate
      * point-in-time figures at the as-of spot rate
    """
    fx = fx or compute.FxBook(trade_currency)
    out = {}
    rep_raw = reporting_currency(series)
    rep, scale = split_currency(rep_raw)
    rep = rep or trade_currency
    converted = rep != trade_currency or scale != 1.0
    spot = fx.spot(rep, as_of)
    if converted:
        out["fcur"] = rep
        if spot is None:
            out["fx_missing"] = 1   # cannot convert: money fields below are left empty rather than shown in the wrong unit
        else:
            out["fx"] = compute.round_sig(spot, 6)

    def conv_avg(value, end, days=365):
        """Flow over the period ending `end` -> trading currency."""
        if value is None:
            return None
        if not converted:
            return value
        rate = fx.average(rep, end - dt.timedelta(days=days), end) or fx.spot(rep, end) or spot
        return None if rate is None else value * scale * rate

    # ---- fiscal years and EPS actuals --------------------------------
    fy = fiscal_year_ends(series)
    if fy:
        out["fy_end"] = fy[-1].isoformat()
    eps_conv, eps_raw = {}, {}
    for day in fy:
        eps = _near(series.get("annualDilutedEPS"), day)
        if eps is None:
            eps = _near(series.get("annualBasicEPS"), day)
        if eps is None:
            ni = _near(series.get("annualNetIncomeCommonStockholders"), day)
            if ni is None:
                ni = _near(series.get("annualNetIncome"), day)
            shares = _near(series.get("annualDilutedAverageShares"), day)
            eps = compute.safe_div(ni, shares)
        if eps is None:
            continue
        label = str(dates.fiscal_label_year(day))
        value = conv_avg(eps, day)
        if value is not None:
            eps_conv[label] = compute.round_sig(value, 5)
            if converted:
                eps_raw[label] = compute.round_sig(eps, 5)
    if eps_conv:
        out["eps_a"] = eps_conv
    if eps_raw:
        out["eps_ar"] = eps_raw

    # ---- valuation (already in the trading currency) -----------------
    mcap_pt = _closest(series.get("trailingMarketCap"), as_of)
    mcap = None
    if mcap_pt:
        mcap = _to_trade(mcap_pt, trade_currency, fx, as_of)
        out["mcap"] = compute.round_sig(mcap, 6)
        out["val_date"] = mcap_pt.date.isoformat()
    ev_pt = _closest(series.get("trailingEnterpriseValue"), as_of)
    if ev_pt:
        out["ev"] = compute.round_sig(_to_trade(ev_pt, trade_currency, fx, as_of), 6)
    for key, name in (("pe", "trailingPeRatio"), ("fpe", "trailingForwardPeRatio"), ("peg", "trailingPegRatio")):
        pt = _closest(series.get(name), as_of)
        if pt and pt.value > 0:
            out[key] = compute.round_sig(pt.value, 5)
    if compute.is_num(price) and out.get("fpe"):
        # Yahoo's forward P/E uses its next-fiscal-year consensus, so price / forward P/E recovers that estimate.
        out["feps"] = compute.round_sig(price / out["fpe"], 5)

    # ---- EBITDA and free cash flow -----------------------------------
    for key, names, annual in (("ebitda", ("trailingNormalizedEBITDA", "trailingEBITDA"), ("annualNormalizedEBITDA", "annualEBITDA")),
                               ("fcf", ("trailingFreeCashFlow",), ("annualFreeCashFlow",))):
        pt, basis = None, "TTM"
        for name in names:
            pt = _latest(series.get(name), as_of, max_age_days=220)
            if pt:
                break
        if pt is None and fy:
            for name in annual:
                value = _near(series.get(name), fy[-1])
                if value is not None and (as_of - fy[-1]).days <= 500:
                    pt, basis = Point(fy[-1], value, rep_raw), "FY"
                    break
        if pt is not None:
            value = conv_avg(pt.value, pt.date)
            if value is not None:
                out[key] = compute.round_sig(value, 6)
                out[key + "_b"] = "%s %s" % (basis, pt.date.isoformat())

    eps_ttm = _latest(series.get("trailingDilutedEPS"), as_of, max_age_days=220)
    if eps_ttm:
        value = conv_avg(eps_ttm.value, eps_ttm.date)
        if value is not None:
            out["eps_ttm"] = compute.round_sig(value, 5)
            # Cross-check our units against Yahoo's own P/E: catches ADR-ratio or currency mix-ups.
            if compute.is_num(price) and value > 0 and out.get("pe"):
                ratio = (price / value) / out["pe"]
                if not 0.7 <= ratio <= 1.43:
                    out["unit_flag"] = 1

    # ---- Altman Z (latest balance sheet + trailing twelve months) ----
    def balance(item):
        pt = _latest(series.get("quarterly" + item), as_of, max_age_days=220)
        if pt:
            return pt.value
        return _near(series.get("annual" + item), fy[-1]) if fy and (as_of - fy[-1]).days <= 500 else None

    def flow(item):
        pt = _latest(series.get("trailing" + item), as_of, max_age_days=220)
        if pt:
            return pt.value
        return _near(series.get("annual" + item), fy[-1]) if fy and (as_of - fy[-1]).days <= 500 else None

    # ---- dividends from the cash-flow statement ----------------------
    # Total cash paid to shareholders / market value. Used when the per-share dividend history has holes
    # (Yahoo's history for many ADRs stops in 2025); a company-level ratio, so no ADR-ratio pitfalls.
    paid, paid_basis = None, None
    pt = _latest(series.get("trailingCashDividendsPaid"), as_of, max_age_days=220)
    if pt:
        paid, paid_basis = pt, "TTM"
    elif fy and (as_of - fy[-1]).days <= 500:
        value = _near(series.get("annualCashDividendsPaid"), fy[-1])
        if value is not None:
            paid, paid_basis = Point(fy[-1], value, rep_raw), "FY"
    if paid is not None and mcap:
        paid_trade = conv_avg(abs(paid.value), paid.date)
        if paid_trade is not None and 0 <= paid_trade / mcap < 0.5:
            out["dy_cf"] = round(paid_trade / mcap, 6)
            out["dy_cf_b"] = "%s %s" % (paid_basis, paid.date.isoformat())

    wc = balance("WorkingCapital")
    if wc is None:
        ca, cl = balance("CurrentAssets"), balance("CurrentLiabilities")
        wc = ca - cl if ca is not None and cl is not None else None
    # Companies reporting under IFRS often have no separate retained-earnings line in this feed.
    # Fall back to everything in equity that is not paid-in capital (retained earnings plus reserves).
    retained, retained_estimated = balance("RetainedEarnings"), False
    if retained is None:
        equity, capital = balance("StockholdersEquity"), balance("CapitalStock")
        if equity is not None and capital is not None:
            retained = equity - capital - (balance("AdditionalPaidInCapital") or 0.0) + (balance("TreasuryStock") or 0.0)
            retained_estimated = True
    mve = None
    if mcap is not None:
        if not converted:
            mve = mcap
        elif spot:
            mve = mcap / (spot * scale)   # market value expressed in the reporting currency
    z, parts, missing = compute.altman_z(wc, balance("TotalAssets"), retained, flow("EBIT"),
                                         mve, balance("TotalLiabilitiesNetMinorityInterest"), flow("TotalRevenue"))
    if z is not None:
        out["z"] = z
        out["zx"] = parts
        if retained_estimated:
            out["z_est"] = 1   # retained earnings approximated by total reserves
    elif missing:
        out["z_miss"] = missing

    # ---- Piotroski F (latest fiscal year vs the one before) ----------
    if len(fy) >= 3:
        share_series = "annualDilutedAverageShares"
        if any(_near(series.get(share_series), d) is None for d in fy[-2:]):
            share_series = "annualOrdinarySharesNumber"
        years = []
        for day in fy[-3:]:
            years.append({
                "ni": _near(series.get("annualNetIncome"), day),
                "cfo": _near(series.get("annualOperatingCashFlow"), day),
                "ta": _near(series.get("annualTotalAssets"), day),
                "ltd": _near(series.get("annualLongTermDebt"), day),
                "ca": _near(series.get("annualCurrentAssets"), day),
                "cl": _near(series.get("annualCurrentLiabilities"), day),
                "shares": _near(series.get(share_series), day),
                "gp": _near(series.get("annualGrossProfit"), day),
                "rev": _near(series.get("annualTotalRevenue"), day),
            })
        # No long-term debt line at all usually means none is carried.
        if all(y["ltd"] is None for y in years) and all(y["ta"] is not None for y in years):
            for y in years:
                y["ltd"] = 0.0
        score, tests = compute.piotroski_f(years)
        if any(t is not None for t in tests):
            out["ft"] = "".join("-" if t is None else str(t) for t in tests)
        if score is not None:
            out["f"] = score
    return out


def _to_trade(point, trade_currency, fx, as_of):
    cur, scale = split_currency(point.currency)
    if not cur or cur == trade_currency:
        return point.value * scale
    rate = fx.spot(cur, as_of)
    return None if rate is None else point.value * scale * rate


def fetch_timeseries(client, symbol, as_of):
    # type: (net.Client, str, dt.date) -> Result
    """Raw statement series (conversion happens later, once exchange rates are known)."""
    resp = client.get(timeseries_url(yahoo_symbol(symbol), as_of))
    if resp.kind == net.NOT_FOUND:
        return Result.no_data("Yahoo has no financial statements for this symbol")
    if not resp.ok:
        return Result.failed(resp)
    try:
        series = parse_timeseries(json.loads(resp.body.decode("utf-8", "replace")))
    except Exception as exc:  # noqa: BLE001
        return Result.broken("unexpected fundamentals format: %s" % type(exc).__name__)
    if not series:
        return Result.no_data("Yahoo has no financial statements for this symbol")
    return Result.ok(series)


# --------------------------------------------------------------------------
# search: sector and industry
# --------------------------------------------------------------------------
def parse_search(payload, symbol):
    quotes = (payload or {}).get("quotes")
    if not isinstance(quotes, list):
        raise ValueError("unexpected search layout")
    want = yahoo_symbol(symbol)
    for q in quotes:
        if (q.get("symbol") or "").upper() == want:
            out = {"ysector": q.get("sectorDisp") or q.get("sector"), "yindustry": q.get("industryDisp") or q.get("industry")}
            return {k: v.replace("—", " - ") for k, v in out.items() if v}
    return None


def fetch_profile(client, symbol):
    # type: (net.Client, str) -> Result
    # Ask for a dozen matches: the exact ticker is not always among the first few, depending on where the job runs.
    params = urllib.parse.urlencode({"q": yahoo_symbol(symbol), "quotesCount": 12, "newsCount": 0, "listsCount": 0})
    resp = client.get(SEARCH_URL % params)
    if resp.kind == net.NOT_FOUND:
        return Result.no_data("symbol not found in Yahoo search")
    if not resp.ok:
        return Result.failed(resp)
    try:
        found = parse_search(json.loads(resp.body.decode("utf-8", "replace")), symbol)
    except Exception as exc:  # noqa: BLE001
        return Result.broken("unexpected search format: %s" % type(exc).__name__)
    if not found:
        return Result.no_data("Yahoo lists no sector/industry for this symbol")
    return Result.ok(found)
