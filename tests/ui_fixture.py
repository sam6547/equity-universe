"""Build a throwaway copy of the dashboard that shows what an outage looks like.

    python3 -m tests.ui_fixture        # writes build/ui-fixture/
    python3 -m http.server 8766 --directory build/ui-fixture

It takes the real latest snapshot and *pretends* that this week's refresh went badly:
fundamentals carried over for many tickers, a few estimates unavailable, one market
figure stale, and the scheduler silent for three days. Nothing here is published -
it exists only to check the warning banners, the stale marks and the week picker.
"""
import copy
import datetime as dt
import json
import shutil
import sys
import zlib

from pipeline import config, dates, health, snapshot

OUT = config.BUILD_DIR / "ui-fixture"


def main():
    real_dates = snapshot.list_snapshot_dates()
    if not real_dates:
        sys.exit("Run the pipeline once first: there is no snapshot to base the fixture on.")
    this_week = real_dates[-1]
    last_week = this_week - dt.timedelta(days=7)
    snap = snapshot.load_snapshot(this_week)

    if OUT.exists():
        shutil.rmtree(str(OUT))
    (OUT / "data" / "snapshots").mkdir(parents=True)
    for name in ("index.html", "styles.css", "app.js"):
        shutil.copy(str(config.SITE_DIR / name), str(OUT / name))

    # "Last week": the same rows, relabelled, with prices nudged so the two weeks differ.
    earlier = copy.deepcopy(snap)
    earlier["as_of"] = last_week.isoformat()
    earlier["generated_at"] = earlier["updated_at"] = "%sT11:40:00Z" % (last_week + dt.timedelta(days=1)).isoformat()
    for row in earlier["rows"]:
        if row.get("price"):
            row["price"] = round(row["price"] * (1 - row.get("chg1w", 0.0)), 2)
    snapshot.write_json(OUT / "data" / "snapshots" / ("%s.json" % last_week.isoformat()), earlier)

    # "This week": pretend Yahoo's fundamentals feed started refusing requests part-way through.
    broken = copy.deepcopy(snap)
    stale_code = "s:%s" % last_week.isoformat()
    refused, unavailable = [], []
    for row in broken["rows"]:
        bucket = zlib.crc32(row["s"].encode()) % 100
        st = row.setdefault("st", {})
        if bucket < 45 and st.get("f") != "n":
            st["f"] = stale_code
            refused.append(row["s"])
        if bucket >= 97 and row.get("eps_e"):
            row.pop("eps_e")
            st["e"] = "m"
            unavailable.append(row["s"])
        if not st:
            row.pop("st")
    first = broken["rows"][0]
    first["ovr"] = {"eps_2026": [6.2, "GuruFocus, read by hand"], "z": [5.61, "GuruFocus"]}
    broken["macro"]["st"] = {"sp500": stale_code}
    broken["overall"] = "failed"
    snapshot.write_json(OUT / "data" / "snapshots" / ("%s.json" % this_week.isoformat()), broken)

    rows = broken["rows"]
    errors = health.ErrorLog()
    for symbol in refused:
        errors.attempt("f")
        errors.add("f", symbol, "blocked", "HTTP 429: request refused (rate limit or block)")
    for symbol in unavailable:
        errors.attempt("e")
        errors.add("e", symbol, "error", "TimeoutError: timed out")
    sources = health.source_reports(rows, errors)
    macro_rep = health.macro_report(broken["macro"], {"sp500": ["Yahoo ^GSPC: HTTP 429: request refused (rate limit or block)",
                                                              "FRED SP500: TimeoutError: timed out"]})
    alerts = ["Enterprise value is filled in for 52% of tickers, down from 90% last week - the source may be returning "
              "empty answers or have changed format."]
    state = health.overall(sources, macro_rep["status"], set(), alerts)
    three_days_ago = dates.iso_ts(dates.utc_now() - dt.timedelta(days=3, hours=2))
    status = {
        "schema": config.SCHEMA_VERSION, "last_check": three_days_ago, "last_refresh": three_days_ago,
        "as_of": this_week.isoformat(), "overall": state,
        "headline": health.headline(state, sources, macro_rep, set(), alerts), "mode": "full", "trigger": "schedule",
        "duration_s": 1420, "tickers": len(rows), "sources": sources, "macro": macro_rep,
        "fx": {"currencies": ["EUR", "GBP", "JPY"], "failed": []},
        "http": {}, "coverage": snapshot.coverage(rows), "coverage_alerts": alerts,
        "attention": health.attention_list(rows), "warnings": ["overrides.csv line 4: unknown field 'colour'"],
        "unavailable": list(health.UNAVAILABLE),
        "links": {"repo": "https://github.com/example/equity-universe",
                  "actions": "https://github.com/example/equity-universe/actions/workflows/refresh.yml",
                  "issues": "https://github.com/example/equity-universe/issues"},
    }
    snapshot.write_json(OUT / "data" / "status.json", status)
    snapshot.write_json(OUT / "data" / "index.json", {
        "schema": 1, "updated": three_days_ago, "latest": this_week.isoformat(),
        "snapshots": [
            {"as_of": this_week.isoformat(), "updated_at": three_days_ago, "overall": state, "rows": len(rows)},
            {"as_of": last_week.isoformat(), "updated_at": earlier["updated_at"], "overall": "ok", "rows": len(rows)},
        ]})
    snapshot.write_json(OUT / "data" / "runs.json", [
        {"ts": three_days_ago, "as_of": this_week.isoformat(), "mode": "full", "overall": state, "headline": status["headline"]},
        {"ts": "%sT11:21:00Z" % (last_week + dt.timedelta(days=2)).isoformat(), "as_of": last_week.isoformat(),
         "mode": "heartbeat", "overall": "ok", "headline": "Week ending %s is complete; nothing to fetch." % last_week.isoformat()},
        {"ts": earlier["updated_at"], "as_of": last_week.isoformat(), "mode": "full", "overall": "ok",
         "headline": "All sources refreshed normally."},
    ])
    print("Wrote %s  (overall: %s; %d tickers with carried-over fundamentals)" % (OUT, state, len(refused)))


if __name__ == "__main__":
    main()
