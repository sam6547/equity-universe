"""Figures entered by hand: spreadsheet-style CSV files dropped into the manual/ folder.

Each file has one row per ticker and whichever columns you have, for example

    Symbol,As of,Source,2025 EPS,2026 EPS,Altman Z-Score,Piotroski F-Score,Dividend Yield
    A,2026-11-16,GuruFocus,4.89,6.20,5.61,7,0.59%
    SNY,2026-11-16,GuruFocus,,,2.33,7,

* A filled cell wins over the fetched value and shows on the dashboard with a pencil mark.
* An empty cell changes nothing.
* Column names are matched loosely ("Market Cap", "Market Capitalization" and "Mkt Cap" all work).
* Files whose name starts with "_" are ignored (templates, notes).

The one-value-per-line overrides.csv is read after these files, so it has the last word.
"""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from . import config, universe

TEXT_FIELDS = ("country", "sector", "industry", "exch")
PERCENT_FIELDS = ("dy", "dg5")
LABELS = {
    "country": "Country", "sector": "Sector", "industry": "Industry", "exch": "Exchange", "price": "Price",
    "mcap": "Market cap", "ev": "Enterprise value", "ebitda": "EBITDA", "fcf": "Free cash flow", "dy": "Dividend yield",
    "dg5": "5-year dividend growth", "z": "Altman Z", "f": "Piotroski F", "feps": "Next-year EPS",
}
_BLANK = ("", "-", "--", "—", "–", "n/a", "na", "#n/a", "nan", "null", "none", "nm")
# Columns of the dashboard's own CSV export that are not figures to enter by hand.
_IGNORED = ("company name", "company", "name", "note", "notes", "comment", "cagr of s p 500", "10 yr treasury yield",
            "inflation cpi", "last fy eps adjusted")
_UNITS = {"k": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12}
_NUMBER = re.compile(r"^\(?\s*([+\-−]?)\s*[$€£]?\s*([+\-−]?)\s*(\d*\.?\d+(?:e[+\-]?\d+)?)\s*"
                     r"(trillion|billion|million|bn|mm|[kmbt])?\b\s*(%)?", re.I)


def normalise(header):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(header or "").lower()).split())


def field_for_header(header):
    # type: (str) -> Optional[str]
    """Column title -> 'symbol' | 'asof' | 'source' | a field name | '' (ignore quietly) | None (not recognised)."""
    h = normalise(header)
    if not h or h in _IGNORED:
        return ""
    if h in ("symbol", "ticker", "symbols", "tickers"):
        return "symbol"
    if h in ("as of", "asof", "date", "as of date", "export date", "week ending"):
        return "asof"
    if h == "source":
        return "source"
    year = re.search(r"\b(20\d\d)\b", h)
    if "eps" in h.split() or "earnings per share" in h:
        if year:
            return "eps_" + year.group(1)
        if "next" in h or "forward" in h:
            return "feps"
        return None
    if h in ("market cap", "market capitalization", "market capitalisation", "mkt cap", "marketcap"):
        return "mcap"
    if h in ("enterprise value", "ev"):
        return "ev"
    if h == "ebitda" or h.startswith("ebitda "):
        return "ebitda"
    if "free cash flow" in h or "free cashflow" in h or h == "fcf":
        return "fcf"
    if "dividend" in h and "growth" in h:
        return "dg5"
    if "dividend yield" in h or h in ("yield", "div yield"):
        return "dy"
    if "altman" in h or h in ("z score", "zscore"):
        return "z"
    if "piotroski" in h or h in ("f score", "fscore", "f value"):
        return "f"
    if h in ("price", "last price", "close", "share price"):
        return "price"
    if h == "exchange":
        return "exch"
    if h in ("country", "sector", "industry"):
        return h
    return None


def parse_value(field, text):
    """Cell text -> value, or None for an empty cell. Raises ValueError when it cannot be read.

    Accepts 1,234.5 / $521.41B (Large Cap) / (3.2) / 2.5% / 0.025. For yields and growth rates a bare
    number above 1 is taken as a percentage, so 2.5 and 2.5% and 0.025 all mean the same thing.
    """
    raw = str(text or "").strip()
    if raw.lower() in _BLANK:
        return None
    if field in TEXT_FIELDS:
        return " ".join(raw.split())
    match = _NUMBER.match(raw.replace(",", ""))
    if not match:
        raise ValueError("'%s' is not a number" % raw[:30])
    sign1, sign2, digits, unit, percent = match.groups()
    # decimal arithmetic so that 521.41B is exactly 521,410,000,000 and not a hair under it
    value = float(decimal.Decimal(digits) * decimal.Decimal(int(_UNITS.get((unit or "").lower(), 1))))
    if sign1 or sign2 or raw.startswith("("):
        value = -value
    if percent or (field in PERCENT_FIELDS and abs(value) > 1):
        value /= 100.0
    if field == "f":
        if value != int(value) or not 0 <= value <= 9:
            raise ValueError("a Piotroski F-Score is a whole number from 0 to 9, not '%s'" % raw[:30])
        return int(value)
    return value


def parse_date(text):
    # type: (str) -> Optional[str]
    raw = str(text or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(raw[:20].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _match_symbol(text, known):
    # type: (str, Dict[str, str]) -> Optional[str]
    base = str(text or "").strip().upper()
    for form in (base, base.replace(".", "/").replace("-", "/"), base.replace("/", ".")):
        if form in known:
            return known[form]
    return None


def read_file(path, known_symbols, label=None):
    # type: (str, Iterable[str], Optional[str]) -> Tuple[List[Tuple[str, str, object, str]], List[str], dict]
    """-> (entries, problems, summary). Entries are (symbol, field, value, note)."""
    label = label or os.path.basename(path)
    known = {s: s for s in known_symbols}
    known.update({s.replace("/", "."): s for s in known_symbols})
    entries, problems = {}, []
    summary = {"file": label, "values": 0, "tickers": 0, "fields": [], "as_of": None, "undated": 0}
    file_date = re.search(r"(20\d\d-\d\d-\d\d)", label)
    file_date = file_date.group(1) if file_date else None

    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return [], ["%s: could not be read (%s) - save it as a CSV file" % (label, exc.__class__.__name__)], summary
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], ["%s: the file is empty" % label], summary

    header = rows[0]
    columns = [field_for_header(h) for h in header]
    if "symbol" not in columns:
        return [], ["%s: no 'Symbol' column found in the first row" % label], summary
    unknown = [h.strip() for h, c in zip(header, columns) if c is None and h.strip()]
    if unknown:
        problems.append("%s: column%s not recognised and ignored: %s" % (label, "" if len(unknown) == 1 else "s", ", ".join(unknown[:8])))
    value_columns = [(i, c) for i, c in enumerate(columns) if c and c not in ("symbol", "asof", "source")]
    if not value_columns:
        return [], problems + ["%s: no columns of figures found - nothing was taken from it" % label], summary
    at = {name: columns.index(name) for name in ("symbol", "asof", "source") if name in columns}

    strangers, bad_cells, dates = [], [], set()
    for line, row in enumerate(rows[1:], start=2):
        def cell(i):
            return row[i].strip() if i < len(row) else ""

        typed = cell(at["symbol"])
        if not typed or typed.startswith("#"):
            continue
        symbol = _match_symbol(typed, known)
        if symbol is None:
            strangers.append(typed)
            continue
        as_of = parse_date(cell(at["asof"])) if "asof" in at else None
        as_of = as_of or file_date
        source = cell(at["source"]) if "source" in at else ""
        note = " · ".join(p for p in (source or label, ("as of %s" % as_of) if as_of else "") if p)
        for i, field in value_columns:
            try:
                value = parse_value(field, cell(i))
            except ValueError as exc:
                bad_cells.append("row %d %s (%s)" % (line, header[i].strip(), exc))
                continue
            if value is None:
                continue
            entries[(symbol, field)] = (symbol, field, value, note)
            if as_of:
                dates.add(as_of)
            else:
                summary["undated"] += 1

    if strangers:
        problems.append("%s: %d ticker%s not in universe.csv and skipped: %s%s" % (
            label, len(strangers), "" if len(strangers) == 1 else "s", ", ".join(strangers[:8]), "…" if len(strangers) > 8 else ""))
    if bad_cells:
        problems.append("%s: %d cell%s could not be read and were skipped: %s%s" % (
            label, len(bad_cells), "" if len(bad_cells) == 1 else "s", "; ".join(bad_cells[:4]), "…" if len(bad_cells) > 4 else ""))

    out = list(entries.values())
    fields = sorted({f for _s, f, _v, _n in out})
    summary.update({"values": len(out), "tickers": len({s for s, _f, _v, _n in out}), "fields": fields,
                    "as_of": max(dates) if dates else None, "oldest": min(dates) if dates else None})
    # Uploading the dashboard's own full export would freeze every figure: say so rather than do it silently.
    if len(known_symbols) >= 20 and summary["tickers"] > 0.5 * len(set(known_symbols)) and len(fields) >= 10:
        problems.append("%s sets %d different columns for most tickers. Those figures now stay as typed and are no longer "
                        "refreshed - delete the columns you did not mean to fix by hand." % (label, len(fields)))
    return out, problems, summary


def load_folder(known_symbols, folder=None):
    # type: (Iterable[str], Optional[object]) -> Tuple[List[Tuple[str, str, object, str]], List[str], List[dict]]
    """Every CSV in manual/ in name order (later files win). A missing folder is fine."""
    folder = str(folder or config.MANUAL_DIR)
    entries, problems, summaries = [], [], []
    if not os.path.isdir(folder):
        return entries, problems, summaries
    known = list(known_symbols)
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".csv") or name.startswith(("_", ".")):
            continue
        got, issues, summary = read_file(os.path.join(folder, name), known, label="manual/" + name)
        entries.extend(got)
        problems.extend(issues)
        summaries.append(summary)
    return entries, problems, summaries


def desired_overrides(entries):
    # type: (Iterable[Tuple[str, str, object, str]]) -> Dict[str, Dict[str, list]]
    """What each row's 'ovr' should hold once these entries are applied (later entries win)."""
    from . import compute
    out = {}
    for symbol, field, value, note in entries:
        if isinstance(value, float):
            value = int(value) if field == "f" else compute.round_sig(value, 7)
        out.setdefault(symbol, {})[field] = [value, note]
    return out
