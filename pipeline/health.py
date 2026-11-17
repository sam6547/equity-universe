"""Turns a finished snapshot plus this run's outcomes into the health report the dashboard shows."""
from __future__ import annotations

import collections
import os
from typing import Dict, Iterable, List, Optional

from . import config, snapshot

# What each dataset is, in the words used on the dashboard.
SOURCES = (
    {"id": "q", "label": "Prices & dividends", "source": "Yahoo Finance (chart data)", "critical": True,
     "fields": "Price, company name, exchange, dividend yield, 5-year dividend growth",
     "fix": "Check finance.yahoo.com is up. If it answers HTTP 429/403 for days, Yahoo is rate-limiting or blocking the "
            "job: lower the request rate in pipeline/config.py (HOST_INTERVALS) or run the job from another network."},
    {"id": "f", "label": "Fundamentals & valuation", "source": "Yahoo Finance (financial statements)", "critical": True,
     "fields": "EPS (actual), market cap, enterprise value, EBITDA, free cash flow, Altman Z, Piotroski F",
     "fix": "Same host as prices. A 'format' error means Yahoo changed its response layout: update "
            "pipeline/sources/yahoo.py (parse_timeseries)."},
    {"id": "p", "label": "Sector & industry", "source": "Yahoo Finance (search)", "critical": False,
     "fields": "Sector, industry (each ticker is re-checked every %d weeks)" % config.PROFILE_REFRESH_WEEKS,
     "fix": "Not urgent: the last known sector/industry stays on screen. See pipeline/sources/yahoo.py (parse_search)."},
    {"id": "e", "label": "EPS estimates", "source": "Nasdaq (analyst earnings forecast)", "critical": False,
     "fields": "Consensus EPS for the next 3-4 fiscal years, exchange-listed stocks only",
     "fix": "Check api.nasdaq.com is up; see pipeline/sources/nasdaq.py (parse_forecast) if the format changed."},
    {"id": "a", "label": "Adjusted EPS, last fiscal year", "source": "Nasdaq (forecast growth figures)", "critical": False,
     "fields": "Last reported year's EPS on the analysts' basis, exchange-listed stocks only",
     "fix": "Check api.nasdaq.com is up; see pipeline/sources/nasdaq.py (parse_growth)."},
    {"id": "n", "label": "Listing & country", "source": "Nasdaq (stock screener)", "critical": False,
     "fields": "Country where the workbook has none; which tickers are exchange-listed",
     "fix": "Check api.nasdaq.com is up; see pipeline/sources/nasdaq.py (parse_screener)."},
)

# The standing list of gaps: what the workbook asks for that an automated job cannot deliver, why,
# what is shown instead, and the legitimate way to get the original figure. Shown on the Data health tab.
UNAVAILABLE = (
    {"name": "GuruFocus \u201cEPS without NRI\u201d for past years",
     "wanted": "Each past year's EPS excluding one-off items, as GuruFocus calculates it",
     "reason": "It is GuruFocus's own calculation and appears only on its pages, which sit behind Cloudflare bot "
               "protection (HTTP 403 to any scheduled job).",
     "instead": "Reported GAAP diluted EPS for each year (Yahoo), plus the latest fiscal year on the analysts' adjusted "
                "basis for exchange-listed tickers (Nasdaq). GAAP figures include one-off items, so they can sit well "
                "below the adjusted ones.",
     "option": "GuruFocus's paid Data API returns EPS without NRI in one request per ticker; or type figures into overrides.csv."},
    {"name": "EPS estimates for every ticker, five years out",
     "wanted": "Consensus EPS for 2026-2030 from GuruFocus, MarketWatch or Yahoo",
     "reason": "Those estimate pages refuse scheduled jobs (Cloudflare, DataDome, and a Yahoo login token that scripts are "
               "not given), and no open feed carries estimates for OTC-traded ADRs.",
     "instead": "Nasdaq's consensus for the next 3-4 fiscal years, exchange-listed tickers only. For OTC tickers: next "
                "year's EPS implied by Yahoo's forward P/E, where Yahoo has one. The fifth year is usually blank.",
     "option": "A licensed estimates feed (GuruFocus, FactSet, LSEG or a market-data API vendor), or hand entry."},
    {"name": "GuruFocus Altman Z-Score and Piotroski F-Score",
     "wanted": "The two scores exactly as GuruFocus publishes them",
     "reason": "Same GuruFocus pages, same bot protection.",
     "instead": "Both are calculated here from Yahoo's statements and usually land close to GuruFocus. Z is blank for "
                "banks, insurers and similar companies (the score needs working capital and EBIT, which they do not "
                "report); F is blank where any of its nine tests cannot be calculated.",
     "option": "GuruFocus's paid Data API (same request as EPS without NRI)."},
    {"name": "Yahoo \u201clevered free cash flow\u201d and Yahoo's own EBITDA",
     "wanted": "The two key-statistics figures as Yahoo displays them",
     "reason": "They are served only to signed-in browser sessions; Yahoo refuses the session token to scripts (HTTP 429). "
               "Yahoo has no public data service.",
     "instead": "Free cash flow (operating cash flow minus capital spending) and EBITDA excluding unusual items, from "
                "Yahoo's statement feed. Same filings, slightly different definitions - expect differences of a few percent.",
     "option": "None automated. Hand entry for the names that matter."},
    {"name": "Country from Yahoo's company profile",
     "wanted": "Head-office country for every ticker",
     "reason": "Same signed-in-only Yahoo data.",
     "instead": "The workbook's own Country label; Nasdaq's listing record where the workbook is blank.",
     "option": "Fill in the workbook's Country column - it always wins."},
    {"name": "Complete dividend history for some ADRs",
     "wanted": "Dividend yield and 5-year dividend growth for every ticker",
     "reason": "Yahoo's per-share dividend history stops in 2025 for many ADRs, mostly Japanese ones; the fuller figures "
               "are on the signed-in pages above.",
     "instead": "Yield from the cash-flow statement (dividends paid divided by market value). Growth is measured up to "
                "the last dividend on record, or left blank where the history is too patchy.",
     "option": "Hand entry, or a licensed dividend feed."},
)


def classify(fail_fraction):
    if fail_fraction > config.DOWN_ABOVE:
        return "down"
    if fail_fraction > config.DEGRADED_ABOVE:
        return "degraded"
    return "ok"


def dataset_counts(rows, dataset):
    # type: (Iterable[dict], str) -> Dict[str, int]
    counts = {"fresh": 0, "cached": 0, "stale": 0, "missing": 0, "nodata": 0}
    for row in rows:
        code = (row.get("st") or {}).get(dataset, "")
        if not code:
            counts["fresh"] += 1
        elif code == "n":
            counts["nodata"] += 1
        elif code == "m":
            counts["missing"] += 1
        elif code.startswith("c:"):
            counts["cached"] += 1
        else:
            counts["stale"] += 1
    return counts


class ErrorLog(object):
    """Collects fetch failures per dataset during a run."""

    def __init__(self):
        self._by_dataset = collections.defaultdict(lambda: collections.OrderedDict())
        self.attempted = collections.Counter()

    def attempt(self, dataset):
        self.attempted[dataset] += 1

    def add(self, dataset, symbol, category, message):
        bucket = self._by_dataset[dataset].setdefault((category, message), [])
        bucket.append(symbol)

    def summary(self, dataset, limit=4):
        items = sorted(self._by_dataset.get(dataset, {}).items(), key=lambda kv: -len(kv[1]))
        return [{"category": cat, "message": msg, "count": len(symbols), "examples": symbols[:8]}
                for (cat, msg), symbols in items[:limit]]

    def total(self, dataset):
        return sum(len(v) for v in self._by_dataset.get(dataset, {}).values())


def source_reports(rows, errors):
    # type: (List[dict], ErrorLog) -> List[dict]
    out = []
    for spec in SOURCES:
        counts = dataset_counts(rows, spec["id"])
        applicable = counts["fresh"] + counts["cached"] + counts["stale"] + counts["missing"]
        failed = counts["stale"] + counts["missing"]
        report = {
            "id": spec["id"], "label": spec["label"], "source": spec["source"], "fields": spec["fields"],
            "status": classify(failed / float(applicable)) if applicable else "ok",
            "counts": counts,
            "attempted": errors.attempted.get(spec["id"], 0),
        }
        found = errors.summary(spec["id"])
        if found:
            report["errors"] = found
        if report["status"] != "ok":
            report["fix"] = spec["fix"]
        out.append(report)
    return out


def coverage_alerts(current, previous):
    # type: (Dict[str, float], Optional[Dict[str, float]]) -> List[str]
    """Flags fields that silently lost coverage (a source answering with empty data looks 'healthy' otherwise)."""
    alerts = []
    labels = dict(snapshot.COVERAGE_FIELDS)
    for key, now in current.items():
        before = (previous or {}).get(key)
        if before is not None and before - now > config.COVERAGE_DROP_ALERT:
            alerts.append("%s is filled in for %d%% of tickers, down from %d%% in the previous snapshot - the source may be returning "
                          "empty answers or have changed format." % (labels.get(key, key), round(now * 100), round(before * 100)))
    return alerts


def attention_list(rows, limit=60):
    """Tickers the source does not recognise at all - usually renamed or delisted."""
    out = []
    for row in rows:
        if (row.get("st") or {}).get("q") == "n":
            out.append({"symbol": row["s"], "issue": "Yahoo Finance has no price history for this symbol - renamed or delisted?"})
        elif (row.get("plag") or 0) >= 15:
            out.append({"symbol": row["s"], "issue": "No trade recorded for %d trading days - delisted, or the ADR programme ended?" % row["plag"]})
    return out[:limit]


def overall(sources, macro_status, fx_failed, alerts, crashed=False):
    if crashed:
        return "failed"
    by_id = {s["id"]: s for s in sources}
    critical = [spec["id"] for spec in SOURCES if spec["critical"]]
    if any(by_id[i]["status"] == "down" for i in critical) or macro_status == "down":
        return "failed"
    if any(s["status"] != "ok" for s in sources) or macro_status != "ok" or fx_failed or alerts:
        return "degraded"
    return "ok"


def headline(status, sources, macro_report, fx_failed, alerts, error=None):
    if error:
        return "The refresh job crashed: %s" % error
    problems = []
    for s in sources:
        if s["status"] == "ok":
            continue
        c = s["counts"]
        bad = c["stale"] + c["missing"]
        total = bad + c["fresh"] + c["cached"]
        reason = s["errors"][0]["message"] if s.get("errors") else "fetch failed"
        problems.append("%s - %s: %d of %d tickers not refreshed (%s)" % (s["label"], s["source"], bad, total, reason))
    for key, item in (macro_report or {}).get("items", {}).items():
        if item["status"] != "ok":
            problems.append("%s not refreshed" % item["label"])
    if fx_failed:
        problems.append("exchange rates unavailable for %s" % ", ".join(sorted(fx_failed)))
    problems.extend(alerts)
    if not problems:
        return "All sources refreshed normally."
    return " | ".join(problems[:4]) + (" | +%d more" % (len(problems) - 4) if len(problems) > 4 else "")


MACRO_LABELS = {"treasury_10y": "10-year Treasury yield", "cpi": "Inflation (CPI)", "sp500": "S&P 500 growth"}


def macro_report(macro, problems):
    # type: (dict, Dict[str, List[str]]) -> dict
    """macro = snapshot['macro'] (values plus 'st' for stale items)."""
    items = {}
    worst = "ok"
    st = macro.get("st") or {}
    for key, label in MACRO_LABELS.items():
        code = st.get(key, "")
        if key in macro and not code:
            status = "ok"
        elif key in macro:
            status, worst = "stale", "degraded"
        else:
            status, worst = "missing", "degraded"
        item = {"label": label, "status": status}
        if code.startswith("s:"):
            item["since"] = code[2:]
        if problems.get(key):
            item["errors"] = problems[key][:4]
        items[key] = item
    if all(i["status"] != "ok" for i in items.values()):
        worst = "down"
    return {"status": worst, "items": items}


def repo_links():
    """Links for the 'how to fix' panel when running inside GitHub Actions."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not server or not repo:
        return {}
    base = "%s/%s" % (server.rstrip("/"), repo)
    links = {"repo": base, "actions": base + "/actions/workflows/refresh.yml", "issues": base + "/issues"}
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        links["run"] = "%s/actions/runs/%s" % (base, run_id)
    return links
