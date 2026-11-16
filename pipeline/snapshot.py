"""Weekly snapshots: one JSON file per week under site/data/snapshots/.

A row is a flat dict per ticker. Values belong to a *dataset* (one upstream call).
When a dataset cannot be fetched, last week's values are carried forward and the
row records that they are stale, so the dashboard can say exactly what is old.

Row status codes, stored in row["st"][dataset] (absent means fresh this week):
    "s:YYYY-MM-DD"  stale - fetch failed, values carried from that week
    "m"             missing - fetch failed and there was nothing to carry
    "n"             the source has no data for this ticker (not an outage)
    "c:YYYY-MM-DD"  cached on purpose (slow-changing data inside its refresh window)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import zlib
from typing import Dict, Iterable, List, Optional, Tuple

from . import compute, config, dates, result as res, universe

# dataset id -> row keys it owns
DATASET_FIELDS = {
    "q": ("yname", "exch", "exch_raw", "cur", "price", "pweek", "plag", "chg1w", "hi52", "lo52", "ret1y",
          "dy", "div_ttm", "dg5", "div_freq", "div_last", "div_lapsed", "div_gap", "div_odd"),
    "f": ("fcur", "fx", "fx_missing", "fy_end", "eps_a", "eps_ar", "mcap", "val_date", "ev", "pe", "fpe", "peg", "feps",
          "ebitda", "ebitda_b", "fcf", "fcf_b", "eps_ttm", "unit_flag", "z", "zx", "z_miss", "f", "ft"),
    "p": ("ysector", "yindustry"),
    "e": ("eps_e",),
    "n": ("ncountry", "nsector", "nindustry", "nmcap"),
}
DATASETS = ("q", "f", "p", "e", "n")
_DERIVED = ("name", "country", "country_alt", "sector", "industry", "src", "mcap_src")


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
def write_json(path, obj):
    """Atomic, compact JSON write."""
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        handle.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(str(path), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def snapshot_path(as_of):
    return config.SNAPSHOT_DIR / ("%s.json" % as_of.isoformat())


def list_snapshot_dates():
    # type: () -> List[dt.date]
    out = []
    if config.SNAPSHOT_DIR.is_dir():
        for name in os.listdir(str(config.SNAPSHOT_DIR)):
            if name.endswith(".json"):
                try:
                    out.append(dates.parse_date(name[:-5]))
                except ValueError:
                    continue
    return sorted(out)


def load_snapshot(as_of):
    data = read_json(snapshot_path(as_of))
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    return data


def load_previous(as_of):
    """The most recent readable snapshot before `as_of`."""
    for day in reversed([d for d in list_snapshot_dates() if d < as_of]):
        snap = load_snapshot(day)
        if snap:
            return snap
    return None


def rows_by_symbol(snap):
    # type: (Optional[dict]) -> Dict[str, dict]
    return {r["s"]: r for r in (snap or {}).get("rows", []) if isinstance(r, dict) and r.get("s")}


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------
def needs_fetch(row, dataset):
    """True when this dataset failed earlier in the week and should be tried again."""
    code = (row.get("st") or {}).get(dataset, "")
    return code == "m" or code.startswith("s:")


def has_values(row, dataset):
    code = (row.get("st") or {}).get(dataset, "")
    return bool(row) and code not in ("m", "n")


def data_date(row, dataset, snap_as_of):
    """Which week the row's values for this dataset come from."""
    code = (row.get("st") or {}).get(dataset, "")
    if code[:2] in ("s:", "c:"):
        return code[2:]
    return snap_as_of


def profile_due(symbol, as_of, base_row, base_as_of):
    """Sector/industry is refreshed every few weeks, staggered so the load is spread evenly."""
    if not base_row or not has_values(base_row, "p") or not base_as_of:
        return True
    age_days = (as_of - dates.parse_date(data_date(base_row, "p", base_as_of))).days
    weeks = config.PROFILE_REFRESH_WEEKS
    if age_days >= (weeks + 1) * 7:
        return True
    slot = (as_of.toordinal() // 7 + zlib.crc32(symbol.encode("utf-8"))) % weeks
    return slot == 0 and age_days >= 7


def clear_dataset(row, dataset):
    for key in DATASET_FIELDS[dataset]:
        row.pop(key, None)
    row.setdefault("st", {}).pop(dataset, None)


def set_fresh(row, dataset, values):
    clear_dataset(row, dataset)
    for key in DATASET_FIELDS[dataset]:
        if values.get(key) is not None:
            row[key] = values[key]


def set_no_data(row, dataset):
    clear_dataset(row, dataset)
    row["st"][dataset] = "n"


def carry(row, dataset, base_row, base_as_of, code="s"):
    """Copy this dataset's values from `base_row`; mark stale (or cached). Returns True if something was carried."""
    if base_row is row:
        return has_values(row, dataset)   # retry of the same week: values and status are already in place
    clear_dataset(row, dataset)
    if not base_row or not base_as_of or not has_values(base_row, dataset):
        row["st"][dataset] = "m"
        return False
    for key in DATASET_FIELDS[dataset]:
        if base_row.get(key) is not None:
            row[key] = base_row[key]
    row["st"][dataset] = "%s:%s" % (code, data_date(base_row, dataset, base_as_of))
    return True


def apply_result(row, dataset, outcome, base_row, base_as_of):
    # type: (dict, str, res.Result, Optional[dict], Optional[str]) -> None
    if outcome.status == res.OK:
        set_fresh(row, dataset, outcome.data)
    elif outcome.status == res.NO_DATA:
        set_no_data(row, dataset)
    else:
        carry(row, dataset, base_row, base_as_of)


def resolve_identity(row, urow):
    """Pick the displayed name / country / sector / industry and remember where each came from."""
    for key in _DERIVED:
        row.pop(key, None)
    src = {}

    row["name"] = row.get("yname") or urow.get("name") or row["s"]

    wb_country = universe.normalize_country(urow.get("country"))
    if wb_country:
        row["country"], src["country"] = wb_country, "wb"
        if row.get("ncountry") and row["ncountry"] != wb_country:
            row["country_alt"] = row["ncountry"]
    elif row.get("ncountry"):
        row["country"], src["country"] = row["ncountry"], "nq"

    wb_sector = universe.normalize_sector(urow.get("sector"))
    if row.get("ysector"):
        row["sector"], src["sector"] = row["ysector"], "y"
    elif wb_sector:
        row["sector"], src["sector"] = wb_sector, "wb"
    elif universe.normalize_sector(row.get("nsector")):
        row["sector"], src["sector"] = universe.normalize_sector(row.get("nsector")), "nq"

    wb_industry = universe.tidy_label(urow.get("industry"))
    if row.get("yindustry"):
        row["industry"], src["industry"] = row["yindustry"], "y"
    elif wb_industry:
        row["industry"], src["industry"] = wb_industry, "wb"
    elif row.get("nindustry"):
        row["industry"], src["industry"] = row["nindustry"], "nq"

    if row.get("mcap") is None and row.get("nmcap"):
        row["mcap"], row["mcap_src"] = compute.round_sig(row["nmcap"], 6), "nq"
    if src:
        row["src"] = src
    if not row.get("st"):
        row.pop("st", None)


# --------------------------------------------------------------------------
# manual overrides
# --------------------------------------------------------------------------
_TEXT_OVERRIDES = ("name", "country", "sector", "industry", "exch")
_NUM_OVERRIDES = ("price", "mcap", "ev", "ebitda", "fcf", "dy", "dg5", "z", "f", "feps")


def load_overrides(path=None):
    # type: (object) -> Tuple[List[Tuple[str, str, object, str]], List[str]]
    """-> ([(symbol, field, value, note)], [problems]). A missing file is fine."""
    path = str(path or config.OVERRIDES_CSV)
    if not os.path.exists(path):
        return [], []
    out, problems = [], []
    with open(path, newline="", encoding="utf-8") as handle:
        for line, raw in enumerate(csv.DictReader(handle), start=2):
            first = (raw.get("symbol") or "").strip()
            if first.startswith("#"):
                continue  # comment line
            symbol = universe.clean_symbol(first)
            field = (raw.get("field") or "").strip().lower()
            text = (raw.get("value") or "").strip()
            if not first and not field and not text:
                continue
            where = "overrides.csv line %d" % line
            if not symbol:
                problems.append("%s: missing or invalid symbol" % where)
                continue
            is_eps = field.startswith("eps_") and field[4:].isdigit() and len(field) == 8
            if field in _TEXT_OVERRIDES:
                value = text
            elif field in _NUM_OVERRIDES or is_eps:
                try:
                    value = float(text.replace(",", "").replace("%", "")) / (100.0 if text.endswith("%") else 1.0)
                except ValueError:
                    problems.append("%s: '%s' is not a number" % (where, text))
                    continue
            else:
                problems.append("%s: unknown field '%s'" % (where, field))
                continue
            if value == "":
                problems.append("%s: empty value" % where)
                continue
            out.append((symbol, field, value, (raw.get("note") or "").strip()))
    return out, problems


def apply_overrides(rows, overrides):
    # type: (Dict[str, dict], Iterable[Tuple[str, str, object, str]]) -> List[str]
    """Attach manual values as row['ovr'] = {field: [value, note]}.

    Fetched values are left untouched underneath, so deleting a line from
    overrides.csv brings the fetched value straight back. The dashboard shows the
    manual value and labels it as such.
    """
    problems = []
    for row in rows.values():
        row.pop("ovr", None)
    for symbol, field, value, note in overrides:
        row = rows.get(symbol)
        if row is None:
            problems.append("overrides.csv: %s is not in universe.csv" % symbol)
            continue
        if isinstance(value, float):
            value = int(value) if field == "f" else compute.round_sig(value, 7)
        row.setdefault("ovr", {})[field] = [value, note]
    return problems


# --------------------------------------------------------------------------
# coverage and index
# --------------------------------------------------------------------------
COVERAGE_FIELDS = (
    ("price", "Price"), ("country", "Country"), ("sector", "Sector"), ("industry", "Industry"), ("exch", "Exchange"),
    ("eps_a", "EPS (actual)"), ("eps_e", "EPS estimates"), ("mcap", "Market cap"), ("ev", "Enterprise value"),
    ("ebitda", "EBITDA"), ("fcf", "Free cash flow"), ("dy", "Dividend yield"), ("dg5", "5-yr dividend growth"),
    ("z", "Altman Z"), ("f", "Piotroski F"),
)


def coverage(rows):
    # type: (Iterable[dict]) -> Dict[str, float]
    rows = list(rows)
    total = max(1, len(rows))
    return {key: round(sum(1 for r in rows if r.get(key) not in (None, {}, "")) / total, 4) for key, _ in COVERAGE_FIELDS}


def eps_years(as_of):
    return list(range(as_of.year - config.EPS_YEARS_BACK, as_of.year + config.EPS_YEARS_FORWARD + 1))


def update_index(entry):
    """Insert/replace this week's entry and drop entries whose file no longer exists."""
    index = read_json(config.INDEX_JSON, {}) or {}
    existing = {e.get("as_of"): e for e in index.get("snapshots", []) if isinstance(e, dict)}
    existing[entry["as_of"]] = entry
    on_disk = {d.isoformat() for d in list_snapshot_dates()}
    snaps = [existing[k] for k in sorted(existing, reverse=True) if k in on_disk]
    out = {"schema": config.SCHEMA_VERSION, "updated": dates.iso_ts(), "latest": snaps[0]["as_of"] if snaps else None,
           "snapshots": snaps}
    write_json(config.INDEX_JSON, out)
    return out
