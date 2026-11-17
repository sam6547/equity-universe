"""Rebuild universe.csv from the Excel workbook.

    python -m pipeline.import_universe "Equity Universe Version 2.xlsx"

Run this on your own computer whenever tickers are added to or removed from the
workbook, then commit universe.csv. (Needs the `openpyxl` package; the weekly job
itself does not.)  Only the ticker, name, country, sector and industry columns are
copied - links and anything else in the workbook stay out of the published repo.
"""
from __future__ import annotations

import sys

from . import config, universe

HEADERS = {"symbol": "symbol", "company name": "name", "country": "country", "sector": "sector", "industry": "industry"}


def read_workbook(path):
    try:
        import openpyxl
    except ImportError:
        raise SystemExit("This one-off import needs openpyxl:  python -m pip install openpyxl")
    sheet = openpyxl.load_workbook(path, data_only=True, read_only=True).worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)
    columns = {}
    for idx, title in enumerate(header):
        key = HEADERS.get(str(title or "").strip().lower())
        if key and key not in columns:
            columns[key] = idx
    if "symbol" not in columns:
        raise SystemExit("Could not find a 'Symbol' column in the first row of %s" % path)

    out, seen, blanks = [], set(), 0
    for raw in rows:
        def cell(key):
            idx = columns.get(key)
            value = raw[idx] if idx is not None and idx < len(raw) else None
            return " ".join(str(value).split()) if value is not None else ""

        symbol = universe.clean_symbol(cell("symbol"))
        if not symbol:
            blanks += 1
            if blanks >= 3:   # the notes/links block under the table starts after a gap
                break
            continue
        blanks = 0
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append({"symbol": symbol, "name": cell("name"), "country": cell("country"),
                    "sector": cell("sector"), "industry": cell("industry")})
    return out


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__)
    rows = read_workbook(argv[1])
    if not rows:
        raise SystemExit("No tickers found in %s" % argv[1])
    # Keep what was decided on the dashboard: tickers hidden there stay hidden.
    try:
        current = {r["symbol"]: r for r in universe.load(include_hidden=True)}
    except (OSError, ValueError):
        current = {}
    in_workbook = {r["symbol"] for r in rows}
    for row in rows:
        row["hidden"] = (current.get(row["symbol"]) or {}).get("hidden", "")
    dropped = sorted(s for s, r in current.items() if s not in in_workbook and not r.get("hidden"))
    universe.save(rows)
    print("Wrote %d tickers to %s (%d hidden)" % (len(rows), config.UNIVERSE_CSV, sum(1 for r in rows if r["hidden"])))
    if dropped:
        print("These tickers were in universe.csv but not in the workbook, and have been dropped "
              "(add them to the workbook, or re-add them on the dashboard): %s" % ", ".join(dropped))


if __name__ == "__main__":
    main(sys.argv)
