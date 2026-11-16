"""A pretend internet for tests: canned answers shaped like the real sources."""
import datetime as dt
import json
import threading
import urllib.parse

from pipeline import dates


def chart_payload(as_of, price=100.0, weeks=560, dividend=0.25, currency="USD", exchange="NYQ", name="Test Co",
                  last_div=None, gmtoffset=-18000):
    """Weekly bars up to and including the week AFTER as_of (so 'future' bars must be ignored)."""
    monday = dates.week_monday(as_of)
    stamps, closes, highs, lows = [], [], [], []
    for i in range(weeks, -2, -1):          # ... as_of week (i=0), following week (i=-1)
        day = monday - dt.timedelta(days=7 * i)
        close = round(price * (1 - 0.0005 * i), 4) if i >= 0 else price * 3   # the future bar is wildly different
        stamps.append(dates.to_epoch(day) + 14 * 3600 + 1800)
        closes.append(close)
        highs.append(close * 1.02)
        lows.append(close * 0.98)
    events = {}
    if dividend:
        last = last_div or (as_of - dt.timedelta(days=30))
        for q in range(36):
            day = last - dt.timedelta(days=91 * q)
            ts = dates.to_epoch(day) + 14 * 3600
            events[str(ts)] = {"amount": round(dividend * 1.05 ** (-(q // 4)), 6), "date": ts}
    result = {
        "meta": {"currency": currency, "symbol": "X", "exchangeName": exchange, "fullExchangeName": exchange,
                 "instrumentType": "EQUITY", "gmtoffset": gmtoffset, "longName": name, "regularMarketPrice": price},
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes, "high": highs, "low": lows}]},
    }
    if events:
        result["events"] = {"dividends": events}
    return {"chart": {"result": [result], "error": None}}


def series_payload(as_of, start_value=100.0, years=31, growth=0.07, gmtoffset=0):
    """An index or currency pair growing at a steady rate, weekly."""
    monday = dates.week_monday(as_of)
    weeks = int(years * 52.2)
    stamps, closes = [], []
    for i in range(weeks, -1, -1):
        day = monday - dt.timedelta(days=7 * i)
        stamps.append(dates.to_epoch(day) + 12 * 3600)
        closes.append(start_value * (1 + growth) ** (-(7 * i) / 365.25))
    return {"chart": {"result": [{"meta": {"currency": "USD", "gmtoffset": gmtoffset}, "timestamp": stamps,
                                  "indicators": {"quote": [{"close": closes, "high": closes, "low": closes}]}}], "error": None}}


def timeseries_payload(series):
    """series = {"annualDilutedEPS": [("2025-12-31", 4.5, "USD"), ...]}"""
    result = []
    for name, points in series.items():
        result.append({
            "meta": {"symbol": ["X"], "type": [name]},
            "timestamp": [dates.to_epoch(dates.parse_date(d)) for d, _v, _c in points],
            name: [{"asOfDate": d, "periodType": "12M", "currencyCode": c, "reportedValue": {"raw": v, "fmt": str(v)}}
                   for d, v, c in points],
        })
    return {"timeseries": {"result": result, "error": None}}


def company_series(as_of, currency="USD", eps=(4.0, 4.4, 4.8, 5.2), scale=1.0, with_retained=True, fy_month_end="12-31",
                   with_equity=False):
    """A plain industrial company: four fiscal years plus trailing and valuation points."""
    year = as_of.year - 1
    fy = ["%d-%s" % (year - 3 + i, fy_month_end) for i in range(4)]
    q = (as_of - dt.timedelta(days=45)).isoformat()
    val = as_of.isoformat()

    def annual(values):
        return [(d, v * scale, currency) for d, v in zip(fy, values)]

    out = {
        "annualTotalRevenue": annual([900, 950, 1000, 1100]),
        "annualGrossProfit": annual([400, 430, 460, 520]),
        "annualEBIT": annual([150, 160, 170, 190]),
        "annualNetIncome": annual([100, 110, 120, 130]),
        "annualNetIncomeCommonStockholders": annual([100, 110, 120, 130]),
        "annualDilutedEPS": [(d, v, currency) for d, v in zip(fy, eps)],
        "annualOperatingCashFlow": annual([140, 150, 160, 180]),
        "annualFreeCashFlow": annual([90, 95, 100, 115]),
        "annualTotalAssets": annual([2000, 2050, 2100, 2150]),
        "annualCurrentAssets": annual([700, 720, 760, 800]),
        "annualCurrentLiabilities": annual([400, 410, 420, 430]),
        "annualTotalLiabilitiesNetMinorityInterest": annual([900, 910, 905, 900]),
        "annualLongTermDebt": annual([400, 390, 380, 360]),
        "annualOrdinarySharesNumber": [(d, v, currency) for d, v in zip(fy, (25.0, 25.0, 25.0, 25.0))],
        "annualDilutedAverageShares": [(d, v, currency) for d, v in zip(fy, (25.0, 25.0, 25.0, 24.8))],
        "annualWorkingCapital": annual([300, 310, 340, 370]),
        "annualEBITDA": annual([200, 215, 230, 255]),
        "annualNormalizedEBITDA": annual([205, 218, 232, 260]),
        "quarterlyTotalAssets": [(q, 2200 * scale, currency)],
        "quarterlyWorkingCapital": [(q, 380 * scale, currency)],
        "quarterlyCurrentAssets": [(q, 820 * scale, currency)],
        "quarterlyCurrentLiabilities": [(q, 440 * scale, currency)],
        "quarterlyTotalLiabilitiesNetMinorityInterest": [(q, 910 * scale, currency)],
        "trailingTotalRevenue": [(q, 1150 * scale, currency)],
        "trailingEBIT": [(q, 200 * scale, currency)],
        "trailingEBITDA": [(q, 265 * scale, currency)],
        "trailingNormalizedEBITDA": [(q, 270 * scale, currency)],
        "trailingFreeCashFlow": [(q, 120 * scale, currency)],
        "trailingDilutedEPS": [(q, 5.4, currency)],
        "trailingMarketCap": [(val, 2500.0, "USD")],
        "trailingEnterpriseValue": [(val, 2800.0, "USD")],
        "trailingPeRatio": [(val, 18.5, None)],
        "trailingForwardPeRatio": [(val, 16.0, None)],
        "trailingPegRatio": [(val, 1.6, None)],
        "annualCashDividendsPaid": annual([-40, -44, -48, -52]),
        "trailingCashDividendsPaid": [(q, -55 * scale, currency)],
    }
    if with_equity:
        out["annualStockholdersEquity"] = annual([1100, 1150, 1200, 1250])
        out["quarterlyStockholdersEquity"] = [(q, 1290 * scale, currency)]
        out["quarterlyCapitalStock"] = [(q, 100 * scale, currency)]
        out["quarterlyAdditionalPaidInCapital"] = [(q, 300 * scale, currency)]
        out["quarterlyTreasuryStock"] = [(q, 50 * scale, currency)]
    if with_retained:
        out["annualRetainedEarnings"] = annual([500, 560, 620, 690])
        out["quarterlyRetainedEarnings"] = [(q, 720 * scale, currency)]
    return out


def search_payload(symbol, sector="Industrials", industry="Specialty Industrial Machinery"):
    return {"quotes": [{"symbol": symbol + "X", "sector": "Other"}, {"symbol": symbol, "sector": sector, "industry": industry}]}


def forecast_payload(first_year, eps=(6.0, 6.6, 7.3, 8.1), month="Dec"):
    rows = [{"fiscalEnd": "%s %d" % (month, first_year + i), "consensusEPSForecast": v, "highEPSForecast": v, "lowEPSForecast": v,
             "noOfEstimates": 8 - 2 * i, "up": 0, "down": 0} for i, v in enumerate(eps)]
    return {"data": {"symbol": "x", "yearlyForecast": {"rows": rows}, "quarterlyForecast": {"rows": []}},
            "message": None, "status": {"rCode": 200}}


def growth_payload(first_estimate_year, growth=20.0, pe_actual=24.0, pe_estimate=20.0):
    """Nasdaq forecast P/E data: last year's P/E on actual earnings, then on estimates, plus expected growth."""
    return {"data": {
        "pegr": {"pegValue": 1.2},
        "per": {"peRatioChart": [{"x": "%d Actual" % (first_estimate_year - 1), "y": pe_actual},
                                 {"x": "%d Estimates" % first_estimate_year, "y": pe_estimate},
                                 {"x": "%d Estimates" % (first_estimate_year + 1), "y": pe_estimate * 0.9}]},
        "gr": {"peGrowthChart": [{"z": "Growth", "x": str(first_estimate_year), "y": growth},
                                 {"z": "Growth", "x": str(first_estimate_year + 1), "y": 10.0},
                                 {"z": "P/E Ratios", "x": str(first_estimate_year), "y": pe_estimate}]},
    }, "message": None, "status": {"rCode": 200}}


FORECAST_UNKNOWN = {"data": None, "message": None,
                    "status": {"rCode": 400, "bCodeMessage": [{"code": 1001, "errorMessage": "Symbol not exists."}]}}


def screener_payload(listed):
    """listed = {"AAA": ("United States", "Industrials", "Machinery", 2.5e9)} plus filler to look realistic."""
    rows = [{"symbol": s, "name": s, "country": c, "sector": sec, "industry": ind, "marketCap": str(cap), "lastsale": "$1"}
            for s, (c, sec, ind, cap) in listed.items()]
    rows += [{"symbol": "F%04d" % i, "name": "Filler", "country": "United States", "sector": "", "industry": "", "marketCap": ""}
             for i in range(1500)]
    return {"data": {"rows": rows}, "status": {"rCode": 200}}


def treasury_csv(as_of, value=4.5):
    lines = ['Date,"1 Mo","2 Yr","10 Yr","30 Yr"']
    for i in range(10):
        day = as_of - dt.timedelta(days=i)
        if day.weekday() < 5:
            lines.append("%s,4.0,4.2,%.2f,4.9" % (day.strftime("%m/%d/%Y"), value + 0.01 * i))
    return "\n".join(lines) + "\n"


def bls_payload(as_of, yoy=0.03, skip=()):
    data = []
    year, month = as_of.year, as_of.month
    for back in range(1, 30):                 # latest published month is last month
        m = month - back
        y = year
        while m <= 0:
            m += 12
            y -= 1
        if (y, m) in skip:
            continue
        value = 300.0 * (1 + yoy) ** (-(back - 1) / 12.0)
        data.append({"year": str(y), "period": "M%02d" % m, "value": "%.3f" % value})
    return {"status": "REQUEST_SUCCEEDED", "message": [], "Results": {"series": [{"seriesID": "CUUR0000SA0", "data": data}]}}


class Internet(object):
    """Routes URLs to canned answers. Flip switches to simulate outages."""

    def __init__(self, as_of):
        self.as_of = as_of
        self.calls = []
        self._lock = threading.Lock()
        self.down = set()            # host buckets answering 429: "yahoo", "nasdaq", "treasury", "bls", "fred"
        self.unknown = set()         # symbols Yahoo has never heard of
        self.otc = set()             # symbols Nasdaq does not cover
        self.eur = set()             # symbols reporting in EUR
        self.no_fundamentals = False  # Yahoo answers with empty statements for everyone
        self.fx_missing = set()      # currencies with no exchange-rate series
        self.listed = {}
        self.price = 100.0

    def count(self, fragment):
        with self._lock:
            return sum(1 for u in self.calls if fragment in u)

    def __call__(self, url, headers, timeout):
        with self._lock:
            self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        path = urllib.parse.unquote(parts.path)

        def js(obj, status=200):
            return status, json.dumps(obj).encode("utf-8"), {}

        if "yahoo" in host:
            if "yahoo" in self.down:
                return 429, b"Too Many Requests", {}
            if "/v8/finance/chart/" in path:
                symbol = path.rsplit("/", 1)[1]
                if symbol.startswith("^"):
                    return js(series_payload(self.as_of, 5000.0, growth=0.10 if "TR" in symbol else 0.07))
                if symbol.endswith("=X"):
                    if symbol[:3] in self.fx_missing or symbol[3:6] in self.fx_missing:
                        return js({"chart": {"result": None, "error": {"code": "Not Found"}}}, 404)
                    return js(series_payload(self.as_of, 1.10, years=7, growth=0.0))
                if symbol in self.unknown:
                    return js({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}, 404)
                exchange = "PNK" if symbol in self.otc else "NYQ"
                return js(chart_payload(self.as_of, price=self.price, exchange=exchange, name="%s Corp" % symbol))
            if "fundamentals-timeseries" in path:
                symbol = path.rsplit("/", 1)[1]
                if self.no_fundamentals or symbol in self.unknown:
                    return js({"timeseries": {"result": [], "error": None}})
                currency = "EUR" if symbol in self.eur else "USD"
                return js(timeseries_payload(company_series(self.as_of, currency=currency)))
            if "/v1/finance/search" in path:
                symbol = urllib.parse.parse_qs(parts.query)["q"][0]
                if symbol in self.unknown:
                    return js({"quotes": []})
                return js(search_payload(symbol))
        if host == "api.nasdaq.com":
            if "nasdaq" in self.down:
                return 429, b"", {}
            if "/screener/" in path:
                return js(screener_payload(self.listed))
            symbol = path.split("/analyst/")[1].split("/")[0]
            if symbol in self.otc or symbol in self.unknown:
                return js(FORECAST_UNKNOWN)
            if path.endswith("/peg-ratio"):
                return js(growth_payload(self.as_of.year))
            return js(forecast_payload(self.as_of.year))
        if host == "home.treasury.gov":
            if "treasury" in self.down:
                return 503, b"", {}
            return 200, treasury_csv(self.as_of).encode("utf-8"), {}
        if host == "api.bls.gov":
            if "bls" in self.down:
                return 503, b"", {}
            return js(bls_payload(self.as_of))
        if host == "fred.stlouisfed.org":
            if "fred" in self.down:
                return 503, b"", {}
            series = urllib.parse.parse_qs(parts.query)["id"][0]
            if series == "DGS10":
                return 200, ("observation_date,DGS10\n%s,4.44\n" % self.as_of.isoformat()).encode("utf-8"), {}
            if series == "CPIAUCNS":
                rows = ["observation_date,CPIAUCNS"]
                for back in range(26, 0, -1):
                    y, m = self.as_of.year, self.as_of.month - back
                    while m <= 0:
                        m += 12
                        y -= 1
                    rows.append("%d-%02d-01,%.3f" % (y, m, 300.0 * 1.03 ** (-(back - 1) / 12.0)))
                return 200, ("\n".join(rows) + "\n").encode("utf-8"), {}
            return 200, b"observation_date,SP500\n", {}
        return 404, b"", {}
