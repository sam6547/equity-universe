"""The ticker list and the clean-up rules for the workbook's hand-typed labels."""
from __future__ import annotations

import csv
import re
from typing import Dict, List, Optional

from . import config

FIELDS = ("symbol", "name", "country", "sector", "industry")
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,9}$")

_COUNTRIES = {
    "UK": "United Kingdom", "U.K.": "United Kingdom", "GREAT BRITAIN": "United Kingdom", "ENGLAND": "United Kingdom",
    "USA": "United States", "US": "United States", "U.S.": "United States", "U.S.A.": "United States",
    "HONGKONG": "Hong Kong", "KOREA": "South Korea", "SOUTH KOREA": "South Korea",
    "DUBAI UNITED ARAB EMIRATES": "United Arab Emirates", "UAE": "United Arab Emirates",
}

# Everything is mapped onto the sector names Yahoo Finance uses.
_SECTORS = {
    "FINANCIAL": "Financial Services", "FINANCIALS": "Financial Services", "FINANCE": "Financial Services",
    "FINANCIAL SERVICES": "Financial Services",
    "DISCRETIONARY": "Consumer Cyclical", "CONSUMER DISCRETIONARY": "Consumer Cyclical", "CONSUMER CYCLICAL": "Consumer Cyclical",
    "STAPLES": "Consumer Defensive", "CONSUMER STAPLES": "Consumer Defensive", "CONSUMER DEFENSIVE": "Consumer Defensive",
    "INDUSTRIAL": "Industrials", "INDUSTRIALS": "Industrials", "INDD": "Industrials",
    "HEALTHCARE": "Healthcare", "HEALTH CARE": "Healthcare",
    "TECHNOLOGY": "Technology", "INFORMATION TECHNOLOGY": "Technology",
    "COMMUNICATION": "Communication Services", "COMMUNICATIONS": "Communication Services",
    "COMMUNICATION SERVICES": "Communication Services", "TELECOMMUNICATIONS": "Communication Services",
    "ENERGY": "Energy",
    "MATERIAL": "Basic Materials", "MATERIALS": "Basic Materials", "BASIC MATERIALS": "Basic Materials",
    "UTILITY": "Utilities", "UTILITIES": "Utilities",
    "REIT": "Real Estate", "REALESTATE": "Real Estate", "REAL ESTATE": "Real Estate",
}


def clean_symbol(value):
    # type: (object) -> Optional[str]
    text = str(value or "").strip().upper()
    return text if _TICKER.match(text) else None


def normalize_country(value):
    # type: (Optional[str]) -> Optional[str]
    text = " ".join(str(value or "").split())
    if not text:
        return None
    mapped = _COUNTRIES.get(text.upper())
    if mapped:
        return mapped
    return text.title() if text.isupper() or text.islower() else text


def normalize_sector(value):
    # type: (Optional[str]) -> Optional[str]
    text = " ".join(str(value or "").split())
    if not text or text.upper() in ("MISCELLANEOUS", "N/A"):
        return None
    return _SECTORS.get(text.upper(), text.title() if text.isupper() else text)


def tidy_label(value):
    # type: (Optional[str]) -> Optional[str]
    """Hand-typed industry labels: collapse spaces, fix ALL-CAPS."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text.title() if text.isupper() and len(text) > 4 else text


def load(path=None):
    # type: (object) -> List[Dict[str, str]]
    """Rows of universe.csv in file order. Raises ValueError if the file is unusable."""
    path = path or config.UNIVERSE_CSV
    rows, seen = [], set()
    with open(str(path), newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "symbol" not in reader.fieldnames:
            raise ValueError("%s needs a 'symbol' column" % path)
        for line, raw in enumerate(reader, start=2):
            symbol = clean_symbol(raw.get("symbol"))
            if not symbol:
                if (raw.get("symbol") or "").strip():
                    raise ValueError("%s line %d: '%s' is not a valid ticker" % (path, line, raw.get("symbol")))
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            row = {"symbol": symbol}
            for key in FIELDS[1:]:
                row[key] = " ".join((raw.get(key) or "").split())
            rows.append(row)
    if not rows:
        raise ValueError("%s contains no tickers" % path)
    return rows


def save(rows, path=None):
    path = path or config.UNIVERSE_CSV
    with open(str(path), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
