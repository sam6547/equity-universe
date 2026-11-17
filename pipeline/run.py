"""The daily refresh.

    python -m pipeline.run                 # normal scheduled run
    python -m pipeline.run --force         # re-fetch everything for the current trading day
    python -m pipeline.run --symbols A,SNY # trial run on a few tickers (writes to build/trial)

How a run decides what to do:
  * The day being reported is the latest US trading day whose close is final
    (from 18:00 New York time; weekends and NYSE holidays are skipped).
  * No snapshot for that day yet    -> full refresh.
  * Snapshot exists with failures   -> retry only what failed.
  * Ticker list or hand-entered figures changed -> fetch only new tickers, rewrite the snapshot.
  * Snapshot exists and is complete -> nothing to fetch; just record that the job is alive.
The schedule runs it several times each morning before the open, so a failed first run is retried.

The job always writes status.json, even when sources fail or the code crashes, so the
dashboard can show what went wrong.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import copy
import datetime as dt
import os
import sys
import time
import traceback

from . import compute, config, dates, health, manual, net, snapshot, universe
from .result import Result, FAILED
from .sources import macro as macro_src
from .sources import nasdaq, yahoo

Plan = collections.namedtuple("Plan", "row base base_as_of todo")
MACRO_KEYS = tuple(health.MACRO_LABELS)


def log(message):
    print(message, flush=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Refresh the equity universe dashboard data.")
    parser.add_argument("--force", action="store_true", help="re-fetch everything for the current trading day")
    parser.add_argument("--as-of", help="report this trading day (YYYY-MM-DD) instead of the latest completed one")
    parser.add_argument("--symbols", help="comma-separated tickers for a trial run")
    parser.add_argument("--limit", type=int, help="only the first N tickers (trial run)")
    parser.add_argument("--data-dir", help="write output here instead of site/data")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    started = time.monotonic()
    trial = bool(args.symbols or args.limit)
    if args.data_dir:
        config.set_data_dir(args.data_dir)
    elif trial:
        config.set_data_dir(config.BUILD_DIR / "trial")
        log("Trial run: writing to %s (the published data is untouched)" % config.DATA_DIR)
    try:
        refresh(args, started)
        return 0
    except Exception as exc:  # noqa: BLE001 - the dashboard must learn about any crash
        traceback.print_exc()
        record_crash(exc, started)
        return 1


# --------------------------------------------------------------------------
def refresh(args, started):
    now = dates.utc_now()
    as_of = dates.parse_date(args.as_of) if args.as_of else dates.last_completed_session(now)
    as_of_iso = as_of.isoformat()
    trigger = os.environ.get("GITHUB_EVENT_NAME") or "local"

    urows = universe.load()
    if args.symbols:
        wanted = {universe.clean_symbol(s) for s in args.symbols.split(",")}
        urows = [u for u in urows if u["symbol"] in wanted]
    if args.limit:
        urows = urows[:args.limit]
    if not urows:
        raise ValueError("no tickers selected")

    existing = None if args.force else snapshot.load_snapshot(as_of)
    previous = snapshot.load_previous(as_of)
    cur_rows = snapshot.rows_by_symbol(existing)
    prev_rows = snapshot.rows_by_symbol(previous)
    prev_as_of = previous.get("as_of") if previous else None

    # ---- plan -----------------------------------------------------------
    plan = collections.OrderedDict()
    for u in urows:
        symbol = u["symbol"]
        current = cur_rows.get(symbol)
        if current is not None:
            row = copy.deepcopy(current)
            row.setdefault("st", {})
            todo = [ds for ds in snapshot.DATASETS if snapshot.needs_fetch(row, ds)]
            plan[symbol] = Plan(row, row, as_of_iso, todo)
        else:
            row = {"s": symbol, "st": {}}
            base = prev_rows.get(symbol)
            todo = list(snapshot.DATASETS)
            if not snapshot.profile_due(symbol, as_of, base, prev_as_of):
                todo.remove("p")
                snapshot.carry(row, "p", base, prev_as_of, code="c")
            plan[symbol] = Plan(row, base, prev_as_of, todo)

    macro = dict((existing or {}).get("macro") or {})
    macro_st = dict(macro.pop("st", None) or {})
    macro_todo = [k for k in MACRO_KEYS if k not in macro or macro_st.get(k)]
    pending = sum(1 for p in plan.values() if p.todo)

    # ---- figures entered by hand ---------------------------------------
    overrides, warnings = snapshot.load_overrides()
    manual_entries, manual_problems, manual_files = manual.load_folder(list(plan))
    hand_entries = manual_entries + overrides          # overrides.csv is read last, so it has the last word
    warnings += manual_problems
    wanted = manual.desired_overrides(e for e in hand_entries if e[0] in plan)
    hand_changed = existing is not None and any(
        (wanted.get(s) or {}) != (cur_rows.get(s, {}).get("ovr") or {}) for s in plan)
    # tickers added, removed or hidden since the snapshot was written
    list_changed = existing is not None and list(plan) != [r.get("s") for r in existing.get("rows", [])]

    if existing is not None and not pending and not macro_todo and not hand_changed and not list_changed:
        heartbeat(as_of_iso, trigger, started)
        return

    # "update": nothing to fetch, but the list or the hand-entered figures changed and the snapshot must show it
    mode = "full" if existing is None else ("retry" if pending or macro_todo else "update")
    log("%s for %s: %d of %d tickers to fetch%s" % (
        mode.capitalize(), as_of_iso, pending, len(plan), ", macro: " + ", ".join(macro_todo) if macro_todo else ""))

    client = net.Client(deadline=started + config.TIME_BUDGET_S)
    errors = health.ErrorLog()

    # ---- market-wide figures -------------------------------------------
    macro_problems = {}
    if macro_todo:
        values, macro_problems = macro_src.fetch_all(client, as_of)
        prev_macro = (previous or {}).get("macro") or {}
        prev_macro_st = prev_macro.get("st") or {}
        for key in MACRO_KEYS:
            if key in values:
                macro[key] = values[key]
                macro_st.pop(key, None)
            elif key not in macro and key in prev_macro:
                macro[key] = prev_macro[key]
                code = prev_macro_st.get(key, "")
                macro_st[key] = code if code.startswith("s:") else "s:%s" % prev_as_of
        log("Macro: %s" % ", ".join("%s=%s" % (k, "ok" if k in values else "FAILED") for k in MACRO_KEYS))

    # ---- exchange listing (one call) -----------------------------------
    listing, listing_error = None, ""
    if any("n" in p.todo for p in plan.values()):
        listing, listing_error = nasdaq.fetch_screener(client)
        log("Nasdaq listing: %s" % ("%d stocks" % len(listing) if listing else "FAILED (%s)" % listing_error))

    def is_listed(symbol, quote_result):
        """Nasdaq only publishes forecasts for exchange-listed stocks; skip the call for OTC tickers."""
        if listing is not None and nasdaq.screener_lookup(listing, symbol) is not None:
            return True
        if quote_result is not None and quote_result.is_ok and quote_result.data.get("exch"):
            return quote_result.data["exch"] in yahoo.LISTED_EXCHANGES
        base = plan[symbol].base or {}
        if base.get("exch") or base.get("eps_e"):
            return base.get("exch") in yahoo.LISTED_EXCHANGES or bool(base.get("eps_e"))
        return len(symbol) <= 4 or "/" in symbol

    # ---- per-ticker fetches --------------------------------------------
    def guarded(fn, *fn_args):
        try:
            return fn(*fn_args)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the run
            return Result.broken("internal error: %s: %s" % (type(exc).__name__, str(exc)[:100]))

    def work(symbol):
        todo = plan[symbol].todo
        out = {}
        if "q" in todo:
            out["q"] = guarded(yahoo.fetch_quote, client, symbol, as_of)
        if "f" in todo:
            out["f"] = guarded(yahoo.fetch_timeseries, client, symbol, as_of)
        if "p" in todo:
            out["p"] = guarded(yahoo.fetch_profile, client, symbol)
        listed = is_listed(symbol, out.get("q")) if ("e" in todo or "a" in todo) else False
        if "e" in todo:
            if listed:
                out["e"] = guarded(nasdaq.fetch_forecast, client, symbol)
            else:
                out["e"] = Result.no_data("OTC-traded: not covered by Nasdaq analyst forecasts")
        if "a" in todo:
            if listed:
                out["a"] = guarded(nasdaq.fetch_growth, client, symbol)
            else:
                out["a"] = Result.no_data("OTC-traded: not covered by Nasdaq analyst forecasts")
        return symbol, out

    results = {}
    targets = [s for s, p in plan.items() if any(ds != "n" for ds in p.todo)]
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.WORKERS) as pool:
        for symbol, out in pool.map(work, targets):
            results[symbol] = out
            done += 1
            if done % 100 == 0 or done == len(targets):
                stats = client.stats()
                log("  %d/%d tickers | %s | %.0fs" % (done, len(targets), ", ".join(
                    "%s: %d ok, %d refused, %d errors" % (name, s["ok"], s["blocked"], s["error"])
                    for name, s in sorted(stats.items())), time.monotonic() - started))

    # ---- exchange rates for companies that report in another currency ---
    books, fx_failed = {}, set()

    def trade_currency(symbol):
        out = results.get(symbol, {})
        if "q" in out and out["q"].is_ok and out["q"].data.get("cur"):
            return out["q"].data["cur"]
        return (plan[symbol].base or {}).get("cur") or "USD"

    needed = set()
    for symbol, out in results.items():
        if "f" in out and out["f"].is_ok:
            rep, _scale = yahoo.split_currency(yahoo.reporting_currency(out["f"].data))
            trade, _ = yahoo.split_currency(trade_currency(symbol))
            if rep and trade and rep != trade:
                needed.add((rep, trade))
    for rep, trade in sorted(needed):
        book = books.setdefault(trade, compute.FxBook(trade))
        points, err = macro_src.fx_series(client, rep, trade, as_of)
        if points:
            book.add(rep, points)
        else:
            fx_failed.add(rep)
            log("  exchange rate %s->%s FAILED: %s" % (rep, trade, err))
    if needed:
        log("Exchange rates: %d currencies, %d failed" % (len(needed), len(fx_failed)))

    # ---- assemble rows --------------------------------------------------
    for u in urows:
        symbol = u["symbol"]
        p = plan[symbol]
        row = p.row
        out = dict(results.get(symbol, {}))

        if "n" in p.todo:
            if listing is None:
                out["n"] = Result(FAILED, None, listing_error or "listing unavailable", "error")
            else:
                entry = nasdaq.screener_lookup(listing, symbol)
                if entry is None:
                    out["n"] = Result.no_data("not listed on NYSE / Nasdaq")
                else:
                    out["n"] = Result.ok({"ncountry": universe.normalize_country(entry.get("country")),
                                          "nsector": entry.get("sector"), "nindustry": entry.get("industry"),
                                          "nmcap": entry.get("mcap")})

        for ds in ("q", "p", "e", "n"):
            if ds in out:
                note(errors, ds, symbol, out[ds])
                snapshot.apply_result(row, ds, out[ds], p.base, p.base_as_of)

        if "f" in out:
            outcome = out["f"]
            if outcome.is_ok:
                outcome = guarded(build_fundamentals, outcome.data, as_of, row, books, fx_failed)
            note(errors, "f", symbol, outcome)
            snapshot.apply_result(row, "f", outcome, p.base, p.base_as_of)

        if "a" in out:
            outcome = out["a"]
            if outcome.is_ok:      # needs this row's estimates (fresh or carried) to turn growth into a figure
                found = nasdaq.adjusted_last_year(outcome.data, row.get("eps_e"))
                outcome = Result.ok({"eps_adj": {found[0]: found[1]}}) if found else \
                    Result.no_data("Nasdaq's figures do not pin down last year's adjusted EPS for this symbol")
            note(errors, "a", symbol, outcome)
            snapshot.apply_result(row, "a", outcome, p.base, p.base_as_of)

        snapshot.resolve_identity(row, u)

    rows_map = collections.OrderedDict((s, p.row) for s, p in plan.items())
    warnings += snapshot.apply_overrides(rows_map, hand_entries)
    rows = list(rows_map.values())
    if hand_entries:
        log("Hand-entered figures: %d from %d file(s) in manual/, %d from overrides.csv" % (
            len(manual_entries), len(manual_files), len(overrides)))

    # ---- health ---------------------------------------------------------
    cov = snapshot.coverage(rows)
    alerts = health.coverage_alerts(cov, (previous or {}).get("coverage"))
    sources = health.source_reports(rows, errors)
    macro_out = dict(macro)
    if macro_st:
        macro_out["st"] = macro_st
    macro_rep = health.macro_report(macro_out, macro_problems)
    state = health.overall(sources, macro_rep["status"], fx_failed, alerts)
    head = health.headline(state, sources, macro_rep, fx_failed, alerts)
    stamp = dates.iso_ts()
    duration = int(time.monotonic() - started)

    snap = {
        "schema": config.SCHEMA_VERSION, "as_of": as_of_iso,
        "generated_at": (existing or {}).get("generated_at") or stamp, "updated_at": stamp,
        "eps_years": snapshot.eps_years(as_of), "macro": macro_out, "coverage": cov, "overall": state, "rows": rows,
    }
    snapshot.write_json(snapshot.snapshot_path(as_of), snap)
    snapshot.update_index({
        "as_of": as_of_iso, "generated_at": snap["generated_at"], "updated_at": stamp, "overall": state, "rows": len(rows),
        "sources": {s["id"]: s["status"] for s in sources},
    })

    status = {
        "schema": config.SCHEMA_VERSION, "last_check": stamp, "last_refresh": stamp, "as_of": as_of_iso,
        "overall": state, "headline": head, "mode": mode, "trigger": trigger, "duration_s": duration,
        "tickers": len(rows), "sources": sources, "macro": macro_rep,
        "fx": {"currencies": sorted(r for r, _ in needed), "failed": sorted(fx_failed)},
        "http": client.stats(), "coverage": cov, "coverage_alerts": alerts,
        "attention": health.attention_list(rows), "warnings": warnings,
        "manual": {"files": manual_files, "overrides_csv": len(overrides), "stale_after_days": config.MANUAL_STALE_DAYS},
        "unavailable": list(health.UNAVAILABLE), "links": health.repo_links(),
        "next_session": dates.next_trading_day(as_of).isoformat(),
    }
    snapshot.write_json(config.STATUS_JSON, status)
    removed = snapshot.prune_snapshots(as_of)
    if removed:
        snapshot.update_index({"as_of": as_of_iso, "generated_at": snap["generated_at"], "updated_at": stamp, "overall": state,
                               "rows": len(rows), "sources": {s["id"]: s["status"] for s in sources}})
        log("Thinned %d daily snapshot(s) older than %d days to one per week." % (len(removed), config.DAILY_RETENTION_DAYS))
    append_run({"ts": stamp, "as_of": as_of_iso, "mode": mode, "trigger": trigger, "overall": state, "headline": head,
                "duration_s": duration, "sources": {s["id"]: s["status"] for s in sources},
                "requests": sum(s["requests"] for s in status["http"].values()), "run": status["links"].get("run")})

    log("")
    log("Result: %s - %s" % (state.upper(), head))
    for s in sources:
        c = s["counts"]
        log("  %-26s %-8s fresh %d, cached %d, stale %d, missing %d, no data %d" % (
            s["label"], s["status"], c["fresh"], c["cached"], c["stale"], c["missing"], c["nodata"]))
    log("  coverage: " + ", ".join("%s %d%%" % (k, round(v * 100)) for k, v in cov.items()))
    for w in warnings:
        log("  warning: %s" % w)
    log("Finished in %ds, %d requests." % (duration, sum(s["requests"] for s in status["http"].values())))


def build_fundamentals(series, as_of, row, books, fx_failed):
    """Statement series -> dashboard fields, converted into the currency the stock trades in."""
    trade, _ = yahoo.split_currency(row.get("cur") or "USD")
    rep, _scale = yahoo.split_currency(yahoo.reporting_currency(series))
    if rep and rep != trade and rep in fx_failed:
        return Result(FAILED, None, "exchange rate %s->%s unavailable" % (rep, trade), "fx")
    fields = yahoo.fundamentals_fields(series, as_of, price=row.get("price"), trade_currency=trade,
                                       fx=books.get(trade) or compute.FxBook(trade))
    if not any(k in fields for k in ("eps_a", "mcap", "ev", "ebitda", "fcf", "z", "f")):
        return Result.no_data("Yahoo has no usable financial statements for this symbol")
    return Result.ok(fields)


def note(errors, dataset, symbol, outcome):
    errors.attempt(dataset)
    if outcome.status == FAILED:
        errors.add(dataset, symbol, outcome.category or "error", outcome.error or "fetch failed")


# --------------------------------------------------------------------------
def heartbeat(as_of_iso, trigger, started):
    """Nothing to fetch: record that the scheduler ran so the dashboard can tell it is alive."""
    stamp = dates.iso_ts()
    status = snapshot.read_json(config.STATUS_JSON, {}) or {}
    status["last_check"] = stamp
    status.setdefault("as_of", as_of_iso)
    status["next_session"] = dates.next_trading_day(dates.parse_date(status["as_of"])).isoformat()
    links = health.repo_links()
    if links:
        status["links"] = links
    snapshot.write_json(config.STATUS_JSON, status)
    append_run({"ts": stamp, "as_of": as_of_iso, "mode": "heartbeat", "trigger": trigger,
                "overall": status.get("overall", "ok"), "headline": "%s is complete; nothing to fetch." % as_of_iso,
                "duration_s": int(time.monotonic() - started), "run": links.get("run")})
    log("%s is already complete - nothing to fetch. Recorded check-in at %s." % (as_of_iso, stamp))


def record_crash(exc, started):
    stamp = dates.iso_ts()
    message = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    try:
        status = snapshot.read_json(config.STATUS_JSON, {}) or {}
        status.update({"schema": config.SCHEMA_VERSION, "last_check": stamp, "overall": "failed",
                       "headline": "The refresh job crashed: %s" % message, "error": message,
                       "mode": "crash", "links": health.repo_links() or status.get("links", {})})
        snapshot.write_json(config.STATUS_JSON, status)
        append_run({"ts": stamp, "as_of": status.get("as_of"), "mode": "crash", "overall": "failed",
                    "trigger": os.environ.get("GITHUB_EVENT_NAME") or "local", "headline": status["headline"],
                    "duration_s": int(time.monotonic() - started), "run": status["links"].get("run")})
    except Exception:  # noqa: BLE001 - nothing more we can do
        traceback.print_exc()


def append_run(entry):
    runs = snapshot.read_json(config.RUNS_JSON, [])
    if not isinstance(runs, list):
        runs = []
    runs.insert(0, {k: v for k, v in entry.items() if v is not None})
    snapshot.write_json(config.RUNS_JSON, runs[:config.RUN_LOG_KEEP])


if __name__ == "__main__":
    sys.exit(main())
