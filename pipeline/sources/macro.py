"""Market-wide figures: 10-year Treasury yield, CPI inflation, S&P 500 growth.

Each figure has an official primary source and a fallback, so one outage does not blank it.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Dict, List, Optional, Tuple

from .. import compute, config, dates, net
from . import yahoo

TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                "%d/all?type=daily_treasury_yield_curve&field_tdr_date_value=%d&page&_format=csv")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s"
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0?startyear=%d&endyear=%d"

# Each item: (value dict or None, list of "source: error" strings for sources that failed)
Item = Tuple[Optional[dict], List[str]]


def _fred_series(client, series_id, start):
    # type: (net.Client, str, dt.date) -> Tuple[Optional[List[Tuple[dt.date, float]]], str]
    resp = client.get(FRED_URL % (series_id, start.isoformat()))
    if not resp.ok:
        return None, resp.describe()
    points = []
    try:
        for row in csv.reader(io.StringIO(resp.body.decode("utf-8", "replace"))):
            if len(row) < 2 or not row[0][:1].isdigit():
                continue
            try:
                points.append((dates.parse_date(row[0]), float(row[1])))
            except ValueError:
                continue  # FRED leaves holidays / unpublished months blank
    except csv.Error as exc:
        return None, "unreadable CSV: %s" % exc
    if not points:
        return None, "no observations returned"
    return points, ""


# --------------------------------------------------------------------------
def parse_treasury_csv(text):
    # type: (str) -> List[Tuple[dt.date, float]]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "10 Yr" not in reader.fieldnames or "Date" not in reader.fieldnames:
        raise ValueError("Treasury CSV is missing the Date / 10 Yr columns")
    points = []
    for row in reader:
        try:
            day = dt.datetime.strptime(row["Date"].strip(), "%m/%d/%Y").date()
            points.append((day, float(row["10 Yr"])))
        except (ValueError, AttributeError, TypeError):
            continue
    return sorted(points)


def treasury_10y(client, as_of):
    # type: (net.Client, dt.date) -> Item
    errors = []
    for year in (as_of.year, as_of.year - 1):
        resp = client.get(TREASURY_URL % (year, year), accept="text/csv, */*")
        if not resp.ok:
            errors.append("U.S. Treasury: %s" % resp.describe())
            break
        try:
            found = compute.value_on_or_before(parse_treasury_csv(resp.body.decode("utf-8-sig", "replace")), as_of, 14)
        except ValueError as exc:
            errors.append("U.S. Treasury: %s" % exc)
            break
        if found:
            return {"value": found[1], "date": found[0].isoformat(), "source": "U.S. Treasury daily par yield curve"}, errors
        # nothing yet this year (first days of January): look at last year
    points, err = _fred_series(client, "DGS10", as_of - dt.timedelta(days=45))
    if points:
        found = compute.value_on_or_before(points, as_of, 14)
        if found:
            return {"value": found[1], "date": found[0].isoformat(), "source": "FRED (DGS10)"}, errors
        err = "no recent observation"
    errors.append("FRED DGS10: %s" % err)
    return None, errors


# --------------------------------------------------------------------------
def cpi_from_monthly(index_by_month, as_of):
    # type: (Dict[Tuple[int, int], float], dt.date) -> Optional[dict]
    """Year-over-year CPI change from {(year, month): index}.

    If the same month a year earlier was never published, it is estimated as the
    geometric mean of its two neighbours and the result says so.
    """
    months = sorted(k for k in index_by_month if k <= (as_of.year, as_of.month))
    if not months:
        return None
    year, month = months[-1]
    if (as_of.year - year) * 12 + (as_of.month - month) > 4:
        return None  # stale beyond a normal publication lag
    latest = index_by_month[(year, month)]
    base = index_by_month.get((year - 1, month))
    note = None
    if base is None:
        before = index_by_month.get((year - 1, month - 1) if month > 1 else (year - 2, 12))
        after = index_by_month.get((year - 1, month + 1) if month < 12 else (year, 1))
        if before is None or after is None:
            return None
        base = (before * after) ** 0.5
        note = "The %d-%02d index was never published; it is estimated from the months around it." % (year - 1, month)
    out = {"yoy": round(latest / base - 1.0, 6), "index": latest, "month": "%d-%02d" % (year, month)}
    if note:
        out["note"] = note
    return out


def cpi(client, as_of):
    # type: (net.Client, dt.date) -> Item
    errors = []
    resp = client.get(BLS_URL % (as_of.year - 2, as_of.year))
    if resp.ok:
        try:
            payload = json.loads(resp.body.decode("utf-8", "replace"))
            if payload.get("status") != "REQUEST_SUCCEEDED":
                raise ValueError("; ".join(payload.get("message") or []) or "request not successful")
            monthly = {}
            for obs in payload["Results"]["series"][0]["data"]:
                period = obs.get("period", "")
                if period.startswith("M") and period != "M13":
                    try:
                        monthly[(int(obs["year"]), int(period[1:]))] = float(obs["value"])
                    except (ValueError, KeyError):
                        continue
            found = cpi_from_monthly(monthly, as_of)
            if found:
                found["source"] = "BLS CPI-U, all items, not seasonally adjusted"
                return found, errors
            errors.append("BLS: no recent CPI observation")
        except Exception as exc:  # noqa: BLE001
            errors.append("BLS: %s" % str(exc)[:160])
    else:
        errors.append("BLS: %s" % resp.describe())

    points, err = _fred_series(client, "CPIAUCNS", dt.date(as_of.year - 2, 1, 1))
    if points:
        found = cpi_from_monthly({(d.year, d.month): v for d, v in points}, as_of)
        if found:
            found["source"] = "FRED (CPIAUCNS)"
            return found, errors
        err = "no recent observation"
    errors.append("FRED CPIAUCNS: %s" % err)
    return None, errors


# --------------------------------------------------------------------------
def sp500(client, as_of):
    # type: (net.Client, dt.date) -> Item
    errors = []
    horizons = config.SP500_HORIZONS
    start = as_of - dt.timedelta(days=int((max(horizons) + 0.2) * 365.25))
    out = None
    price, err = yahoo.fetch_series(client, "^GSPC", start, as_of)
    if price:
        end = compute.value_on_or_before(price, as_of, 10)
        growth = compute.index_cagrs(price, as_of, horizons)
        if end and growth:
            out = {"level": compute.round_sig(end[1], 7), "date": end[0].isoformat(), "cagr": {"price": growth},
                   "source": "Yahoo Finance (^GSPC daily closes)"}
        else:
            err = "price history too short"
    if out is None:
        errors.append("Yahoo ^GSPC: %s" % err)
        points, err = _fred_series(client, "SP500", as_of - dt.timedelta(days=int(10.2 * 365.25)))
        if points:
            end = compute.value_on_or_before(points, as_of, 10)
            growth = compute.index_cagrs(points, as_of, [h for h in horizons if h <= 10])
            if end and growth:
                out = {"level": end[1], "date": end[0].isoformat(), "cagr": {"price": growth},
                       "source": "FRED (SP500, last 10 years only)"}
            else:
                err = "price history too short"
        if out is None:
            errors.append("FRED SP500: %s" % err)
            return None, errors

    total, err = yahoo.fetch_series(client, "^SP500TR", start, as_of)
    if total:
        growth = compute.index_cagrs(total, as_of, horizons)
        if growth:
            out["cagr"]["tr"] = growth
    else:
        errors.append("Yahoo ^SP500TR (total return): %s" % err)

    head = config.SP500_HEADLINE
    value = out["cagr"].get(head["series"], {}).get(str(head["years"]))
    if value is None:  # configured figure unavailable: fall back to the longest price horizon we have
        avail = sorted(out["cagr"]["price"], key=int)
        head = {"series": "price", "years": int(avail[-1])}
        value = out["cagr"]["price"][avail[-1]]
    out["headline"] = {"series": head["series"], "years": head["years"], "value": value}
    return out, errors


# --------------------------------------------------------------------------
def fx_series(client, currency, base, as_of):
    # type: (net.Client, str, str, dt.date) -> Tuple[Optional[List[Tuple[dt.date, float]]], str]
    """Daily rates: units of `base` per one unit of `currency`."""
    start = as_of - dt.timedelta(days=int(6.2 * 365.25))
    points, err = yahoo.fetch_series(client, "%s%s=X" % (currency, base), start, as_of)
    if points:
        return points, ""
    inverse, err2 = yahoo.fetch_series(client, "%s%s=X" % (base, currency), start, as_of)
    if inverse:
        return [(d, 1.0 / v) for d, v in inverse if v > 0], ""
    return None, err or err2


def fetch_all(client, as_of):
    # type: (net.Client, dt.date) -> Tuple[Dict[str, dict], Dict[str, List[str]]]
    """-> ({item: value}, {item: [errors]}). Items that could not be fetched are absent from the first dict."""
    values, problems = {}, {}
    for key, fn in (("treasury_10y", treasury_10y), ("cpi", cpi), ("sp500", sp500)):
        try:
            value, errors = fn(client, as_of)
        except Exception as exc:  # noqa: BLE001 - a bug in one item must not take down the others
            value, errors = None, ["unexpected error: %s: %s" % (type(exc).__name__, str(exc)[:160])]
        if value is not None:
            values[key] = value
        if errors:
            problems[key] = errors
    return values, problems
