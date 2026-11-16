"""Nasdaq.com public JSON.

  earnings-forecast   consensus EPS for the next 3-4 fiscal years (exchange-listed stocks only)
  screener            one call listing every NYSE/Nasdaq/NYSE American stock with its country

OTC-traded ADRs are not covered: Nasdaq answers "Symbol not exists" for them.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Dict, Optional

from .. import compute, dates, net
from ..result import Result

FORECAST_URL = "https://api.nasdaq.com/api/analyst/%s/earnings-forecast"
GROWTH_URL = "https://api.nasdaq.com/api/analyst/%s/peg-ratio"
SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"


def forecast_symbol(symbol):
    """Workbook 'BF/B' -> Nasdaq 'BF.B'."""
    return symbol.strip().upper().replace("/", ".").replace("-", ".")


def screener_lookup(listing, symbol):
    """Find a ticker in the screener listing, which writes share classes as either 'BF/B' or 'LEN.B'."""
    base = symbol.strip().upper()
    for form in (base, base.replace("-", "/").replace(".", "/"), base.replace("-", ".").replace("/", ".")):
        if form in listing:
            return listing[form]
    return None


def parse_forecast(payload):
    """-> {'2026': [consensus_eps, analysts], ...} or None when Nasdaq has no forecast.

    Raises ValueError on an unknown layout.
    """
    if not isinstance(payload, dict) or "status" not in payload:
        raise ValueError("unexpected forecast layout")
    data = payload.get("data")
    if not data:
        return None  # e.g. status 400 "Symbol not exists."
    if not isinstance(data, dict) or "yearlyForecast" not in data:
        raise ValueError("forecast is missing yearlyForecast")
    yearly = data["yearlyForecast"]
    if yearly is None:
        return None  # known symbol with no analyst coverage (small caps, funds)
    if not isinstance(yearly, dict):
        raise ValueError("yearlyForecast has an unexpected shape")
    out = {}
    for row in yearly.get("rows") or []:
        end = dates.parse_month_year(row.get("fiscalEnd"))
        eps = _num(row.get("consensusEPSForecast"))
        if end is None or eps is None:
            continue
        entry = [compute.round_sig(eps, 5)]
        analysts = _num(row.get("noOfEstimates"))
        if analysts is not None:
            entry.append(int(analysts))
        out[str(dates.fiscal_label_year(end))] = entry
    return out or None


def _num(value):
    if compute.is_num(value):
        return float(value)
    if isinstance(value, str):
        text = value.replace("$", "").replace(",", "").strip()
        try:
            number = float(text)
        except ValueError:
            return None
        return number if compute.is_num(number) else None
    return None


def fetch_forecast(client, symbol):
    # type: (net.Client, str) -> Result
    resp = client.get(FORECAST_URL % urllib.parse.quote(forecast_symbol(symbol), safe=""))
    if resp.kind == net.NOT_FOUND:
        return Result.no_data("not covered by Nasdaq analyst forecasts")
    if not resp.ok:
        return Result.failed(resp)
    try:
        eps = parse_forecast(json.loads(resp.body.decode("utf-8", "replace")))
    except Exception as exc:  # noqa: BLE001
        return Result.broken("unexpected forecast format: %s" % type(exc).__name__)
    if not eps:
        return Result.no_data("Nasdaq has no analyst forecast for this symbol")
    return Result.ok({"eps_e": eps})


def parse_growth(payload):
    """Nasdaq's forecast P/E page data -> {"actual_year": "2025", "pe": {"2025": 30.94, ...}, "growth": {"2026": 10.87}}.

    The first P/E is on the last *reported* fiscal year's earnings, the rest on consensus estimates,
    and "growth" is the expected change in EPS for each estimate year. Returns None when Nasdaq has nothing.
    Raises ValueError on an unknown layout.
    """
    if not isinstance(payload, dict) or "status" not in payload:
        raise ValueError("unexpected growth layout")
    data = payload.get("data")
    if not data:
        return None
    if not isinstance(data, dict) or "per" not in data or "gr" not in data:
        raise ValueError("growth data is missing its P/E or growth section")
    out = {"actual_year": None, "pe": {}, "growth": {}}
    for item in ((data.get("per") or {}).get("peRatioChart")) or []:
        label = str(item.get("x") or "").split()
        value = _num(item.get("y"))
        if len(label) == 2 and label[0].isdigit() and value:
            out["pe"][label[0]] = value
            if label[1].lower().startswith("actual"):
                out["actual_year"] = label[0]
    for item in ((data.get("gr") or {}).get("peGrowthChart")) or []:
        year, value = str(item.get("x") or ""), _num(item.get("y"))
        if item.get("z") == "Growth" and year.isdigit() and value is not None:
            out["growth"][year] = value
    if not out["pe"] and not out["growth"]:
        return None
    return out


def adjusted_last_year(growth, estimates):
    """Last reported fiscal year's EPS on the analysts' (adjusted) basis -> ("2025", 5.59) or None.

    Nasdaq does not print that figure, but it publishes two things that pin it down:
      * expected EPS growth for the first estimate year:  actual = estimate / (1 + growth)
      * P/E on actual and on estimated earnings:           actual = estimate * PE_estimate / PE_actual
    The value is only returned when a method is usable and, where both are, they agree within 2%.
    """
    if not growth or not estimates:
        return None
    first = min((y for y in estimates if y.isdigit()), key=int, default=None)
    if first is None:
        return None
    estimate = estimates[first][0]
    actual_year = growth.get("actual_year") or str(int(first) - 1)
    if int(actual_year) != int(first) - 1 or not compute.is_num(estimate) or estimate <= 0:
        return None
    candidates = []
    rate = growth["growth"].get(first)
    if rate is not None and rate > -95:
        candidates.append(estimate / (1.0 + rate / 100.0))
    pe_actual, pe_estimate = growth["pe"].get(actual_year), growth["pe"].get(first)
    if pe_actual and pe_estimate and pe_actual > 0 and pe_estimate > 0:
        candidates.append(estimate * pe_estimate / pe_actual)
    candidates = [c for c in candidates if compute.is_num(c) and c > 0]
    if not candidates:
        return None
    if len(candidates) == 2 and abs(candidates[0] - candidates[1]) > 0.02 * max(candidates):
        return None   # the two routes disagree: do not publish a guess
    return actual_year, compute.round_sig(candidates[0], 4)


def fetch_growth(client, symbol):
    # type: (net.Client, str) -> Result
    resp = client.get(GROWTH_URL % urllib.parse.quote(forecast_symbol(symbol), safe=""))
    if resp.kind == net.NOT_FOUND:
        return Result.no_data("not covered by Nasdaq analyst forecasts")
    if not resp.ok:
        return Result.failed(resp)
    try:
        parsed = parse_growth(json.loads(resp.body.decode("utf-8", "replace")))
    except Exception as exc:  # noqa: BLE001
        return Result.broken("unexpected growth format: %s" % type(exc).__name__)
    if not parsed:
        return Result.no_data("Nasdaq has no forecast growth figures for this symbol")
    return Result.ok(parsed)


def parse_screener(payload):
    # type: (dict) -> Dict[str, Dict[str, object]]
    rows = ((payload or {}).get("data") or {}).get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("unexpected screener layout")
    out = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or "^" in symbol:  # skip preferred shares
            continue
        entry = {}
        for key in ("country", "sector", "industry"):
            value = (row.get(key) or "").strip()
            if value:
                entry[key] = value
        mcap = _num(row.get("marketCap"))
        if mcap:
            entry["mcap"] = mcap
        out[symbol] = entry
    if len(out) < 1000:
        raise ValueError("screener returned only %d stocks" % len(out))
    return out


def fetch_screener(client):
    # type: (net.Client) -> (Optional[Dict[str, Dict[str, object]]], str)
    """-> (listing or None, error message)."""
    resp = client.get(SCREENER_URL)
    if not resp.ok:
        return None, resp.describe()
    try:
        return parse_screener(json.loads(resp.body.decode("utf-8", "replace"))), ""
    except Exception as exc:  # noqa: BLE001
        return None, "unexpected screener format: %s" % exc
