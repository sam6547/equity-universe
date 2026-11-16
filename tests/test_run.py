"""End-to-end runs against a pretend internet: outages, retries, carry-forward, crash reporting."""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from pipeline import config, health, run, snapshot

from . import fakes

D = dt.date
WEEK1, WEEK2, WEEK3, WEEK4 = D(2026, 11, 13), D(2026, 11, 20), D(2026, 11, 27), D(2026, 12, 4)

UNIVERSE = """symbol,name,country,sector,industry
AAA,Alpha Inc,USA,INDUSTRIAL,Machines
BBB,Beta SA,FRANCE,,
CCCCY,Gamma KK,JAPAN,Financial ,Insurance - Life
DDD,Delisted Corp,,,
EEE,Epsilon Ltd,,,Widgets
"""


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash-test-")
        self.saved = {k: getattr(config, k) for k in
                      ("UNIVERSE_CSV", "OVERRIDES_CSV", "DATA_DIR", "SNAPSHOT_DIR", "INDEX_JSON", "STATUS_JSON", "RUNS_JSON")}
        config.UNIVERSE_CSV = config.Path(self.tmp) / "universe.csv"
        config.OVERRIDES_CSV = config.Path(self.tmp) / "overrides.csv"
        config.set_data_dir(os.path.join(self.tmp, "data"))
        with open(str(config.UNIVERSE_CSV), "w") as handle:
            handle.write(UNIVERSE)
        self.net = fakes.Internet(WEEK1)
        self.net.unknown = {"DDD"}
        self.net.otc = {"CCCCY"}
        self.net.eur = {"BBB"}
        self.net.listed = {"AAA": ("United States", "Industrials", "Machinery", 2.5e9),
                           "BBB": ("France", "Health Care", "Pharma", 9e9),
                           "EEE": ("Canada", "Technology", "Software", 1e9)}
        patches = [mock.patch("pipeline.net._default_transport", self.net),
                   mock.patch("pipeline.net.time.sleep", lambda s: None),
                   mock.patch("pipeline.run.log", lambda m: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for key, value in self.saved.items():
            setattr(config, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def go(self, week, *extra):
        self.net.as_of = week
        return run.main(["--as-of", week.isoformat(), "--data-dir", str(config.DATA_DIR)] + list(extra))

    def snap(self, week):
        return snapshot.load_snapshot(week)

    def row(self, week, symbol):
        return snapshot.rows_by_symbol(self.snap(week))[symbol]

    def status(self):
        return snapshot.read_json(config.STATUS_JSON)

    def source(self, dataset):
        return {s["id"]: s for s in self.status()["sources"]}[dataset]


class HealthyRunTest(PipelineCase):
    def test_full_refresh_writes_everything(self):
        self.assertEqual(self.go(WEEK1), 0)
        snap = self.snap(WEEK1)
        self.assertEqual([r["s"] for r in snap["rows"]], ["AAA", "BBB", "CCCCY", "DDD", "EEE"])   # workbook order kept
        self.assertEqual(snap["eps_years"], list(range(2023, 2031)))

        a = self.row(WEEK1, "AAA")
        self.assertEqual(a["price"], 100.0)
        self.assertEqual(a["name"], "AAA Corp")
        self.assertEqual((a["country"], a["sector"], a["industry"]), ("United States", "Industrials", "Specialty Industrial Machinery"))
        self.assertEqual(a["src"], {"country": "wb", "sector": "y", "industry": "y"})
        self.assertEqual(a["eps_a"]["2025"], 5.2)
        self.assertEqual(a["eps_e"]["2026"], [6.0, 8])
        self.assertEqual(a["f"], 9)
        self.assertNotIn("st", a)

        b = self.row(WEEK1, "BBB")                    # reports in EUR
        self.assertEqual(b["fcur"], "EUR")
        self.assertAlmostEqual(b["eps_a"]["2025"], 5.72, places=2)

        c = self.row(WEEK1, "CCCCY")                  # OTC: no Nasdaq coverage, never even asked
        self.assertEqual(c["st"], {"e": "n", "n": "n"})
        self.assertEqual(c["exch"], "OTC")
        self.assertEqual(c["country"], "Japan")
        self.assertEqual(self.net.count("/analyst/CCCCY/"), 0)

        e = self.row(WEEK1, "EEE")                    # blank in the workbook: filled from Nasdaq
        self.assertEqual((e["country"], e["src"]["country"]), ("Canada", "nq"))

        d = self.row(WEEK1, "DDD")                    # unknown to Yahoo: flagged for a human, not an outage
        self.assertEqual(d["st"]["q"], "n")
        self.assertEqual(d["name"], "Delisted Corp")
        status = self.status()
        self.assertEqual(status["overall"], "ok")
        self.assertEqual([x["symbol"] for x in status["attention"]], ["DDD"])
        self.assertEqual(status["mode"], "full")

        macro = snap["macro"]
        self.assertAlmostEqual(macro["treasury_10y"]["value"], 4.5)
        self.assertAlmostEqual(macro["cpi"]["yoy"], 0.03, places=3)
        self.assertAlmostEqual(macro["sp500"]["cagr"]["price"]["10"], 0.07, places=3)
        self.assertAlmostEqual(macro["sp500"]["cagr"]["tr"]["10"], 0.10, places=3)
        self.assertEqual(macro["sp500"]["headline"]["years"], 10)

        index = snapshot.read_json(config.INDEX_JSON)
        self.assertEqual(index["latest"], WEEK1.isoformat())
        self.assertEqual(len(snapshot.read_json(config.RUNS_JSON)), 1)

    def test_second_run_in_the_same_week_only_checks_in(self):
        self.go(WEEK1)
        before_calls = len(self.net.calls)
        before = self.snap(WEEK1)
        first_check = self.status()["last_check"]
        with mock.patch("pipeline.dates.iso_ts", lambda moment=None: "2026-11-15T11:17:00Z"):
            self.assertEqual(self.go(WEEK1), 0)
        self.assertEqual(len(self.net.calls), before_calls)               # no traffic at all
        self.assertEqual(self.snap(WEEK1), before)
        status = self.status()
        self.assertEqual(status["last_check"], "2026-11-15T11:17:00Z")
        self.assertNotEqual(status["last_check"], first_check)
        self.assertEqual(status["overall"], "ok")
        runs = snapshot.read_json(config.RUNS_JSON)
        self.assertEqual([r["mode"] for r in runs], ["heartbeat", "full"])

    def test_force_refetches(self):
        self.go(WEEK1)
        calls = len(self.net.calls)
        self.net.price = 111.0
        self.go(WEEK1, "--force")
        self.assertGreater(len(self.net.calls), calls)
        self.assertEqual(self.row(WEEK1, "AAA")["price"], 111.0)

    def test_sector_lookup_is_cached_between_weeks(self):
        self.go(WEEK1)
        self.assertEqual(self.net.count("/v1/finance/search"), 5)
        self.go(WEEK2)
        self.assertLess(self.net.count("/v1/finance/search"), 10)         # not everyone is re-checked every week
        row = self.row(WEEK2, "AAA")
        self.assertEqual(row["sector"], "Industrials")
        cached = [r for r in self.snap(WEEK2)["rows"] if (r.get("st") or {}).get("p", "").startswith("c:")]
        self.assertTrue(cached)
        self.assertEqual(self.status()["overall"], "ok")                  # cached is healthy, not stale


class OutageTest(PipelineCase):
    def test_yahoo_down_keeps_last_week_and_says_so(self):
        self.go(WEEK1)
        self.net.down = {"yahoo"}
        self.net.price = 150.0
        yahoo_calls = self.net.count("yahoo.com")
        self.assertEqual(self.go(WEEK2), 0)                               # the job itself still succeeds

        a = self.row(WEEK2, "AAA")
        self.assertEqual(a["price"], 100.0)                               # last good value, not blank
        self.assertEqual(a["st"]["q"], "s:2026-11-13")
        self.assertEqual(a["st"]["f"], "s:2026-11-13")
        self.assertEqual(a["eps_a"]["2025"], 5.2)
        self.assertNotIn("e", a["st"])                                    # Nasdaq was fine
        self.assertEqual(a["eps_e"]["2026"], [6.0, 8])

        status = self.status()
        self.assertEqual(status["overall"], "failed")
        self.assertEqual(self.source("q")["status"], "down")
        self.assertEqual(self.source("f")["status"], "down")
        self.assertEqual(self.source("e")["status"], "ok")
        self.assertIn("fix", self.source("q"))
        self.assertIn("refus", status["headline"])
        self.assertTrue(status["http"]["yahoo"]["open"])                  # breaker opened
        self.assertLess(self.net.count("yahoo.com") - yahoo_calls, 40)    # ... so Yahoo was not hammered
        self.assertEqual(snapshot.read_json(config.INDEX_JSON)["latest"], WEEK2.isoformat())

        # S&P 500 figures also come from Yahoo -> carried and marked stale; Treasury/BLS were fine.
        macro = self.snap(WEEK2)["macro"]
        self.assertEqual(macro["st"], {"sp500": "s:2026-11-13"})
        self.assertEqual(status["macro"]["items"]["sp500"]["status"], "stale")
        self.assertEqual(status["macro"]["items"]["treasury_10y"]["status"], "ok")

    def test_retry_next_day_heals_only_what_failed(self):
        self.go(WEEK1)
        self.net.down = {"yahoo"}
        self.go(WEEK2)
        self.net.down = set()
        self.net.price = 150.0
        nasdaq_calls = self.net.count("api.nasdaq.com")
        self.assertEqual(self.go(WEEK2), 0)

        a = self.row(WEEK2, "AAA")
        self.assertEqual(a["price"], 150.0)
        self.assertNotIn("st", a)
        status = self.status()
        self.assertEqual(status["mode"], "retry")
        self.assertEqual(status["overall"], "ok")
        self.assertEqual(self.net.count("api.nasdaq.com"), nasdaq_calls)  # healthy data was not fetched again
        self.assertNotIn("st", self.snap(WEEK2)["macro"])
        self.assertEqual([r["mode"] for r in snapshot.read_json(config.RUNS_JSON)], ["retry", "full", "full"])

    def test_stale_date_stays_the_week_the_data_is_really_from(self):
        self.go(WEEK1)
        self.net.down = {"yahoo"}
        self.go(WEEK2)
        self.go(WEEK3)
        self.go(WEEK4)
        self.assertEqual(self.row(WEEK4, "AAA")["st"]["q"], "s:2026-11-13")
        self.assertEqual(self.snap(WEEK4)["macro"]["st"]["sp500"], "s:2026-11-13")

    def test_first_ever_run_with_source_down_reports_missing(self):
        self.net.down = {"yahoo"}
        self.assertEqual(self.go(WEEK1), 0)
        a = self.row(WEEK1, "AAA")
        self.assertEqual(a["st"]["q"], "m")
        self.assertNotIn("price", a)
        self.assertEqual(a["name"], "Alpha Inc")                          # workbook name still shown
        self.assertEqual(self.status()["overall"], "failed")
        self.assertEqual(self.status()["macro"]["items"]["sp500"]["status"], "missing")

    def test_nasdaq_down_is_degraded_not_failed(self):
        self.go(WEEK1)
        self.net.down = {"nasdaq"}
        self.go(WEEK2)
        status = self.status()
        self.assertEqual(status["overall"], "degraded")
        self.assertEqual(self.source("e")["status"], "down")
        self.assertEqual(self.source("n")["status"], "down")
        a = self.row(WEEK2, "AAA")
        self.assertEqual(a["eps_e"]["2026"], [6.0, 8])                    # carried
        self.assertEqual(a["st"]["e"], "s:2026-11-13")
        self.assertEqual(a["price"], 100.0)
        self.assertNotIn("q", a["st"])
        e = self.row(WEEK2, "EEE")
        self.assertEqual(e["country"], "Canada")                          # country carried too

    def test_macro_fallbacks(self):
        self.net.down = {"treasury", "bls"}
        self.go(WEEK1)
        macro = self.snap(WEEK1)["macro"]
        self.assertEqual(macro["treasury_10y"]["source"], "FRED (DGS10)")
        self.assertAlmostEqual(macro["treasury_10y"]["value"], 4.44)
        self.assertEqual(macro["cpi"]["source"], "FRED (CPIAUCNS)")
        self.assertAlmostEqual(macro["cpi"]["yoy"], 0.03, places=3)
        items = self.status()["macro"]["items"]
        self.assertEqual(items["treasury_10y"]["status"], "ok")           # value obtained, but the outage is on record
        self.assertTrue(any("Treasury" in e for e in items["treasury_10y"]["errors"]))

    def test_silent_empty_answers_raise_a_coverage_alert(self):
        self.go(WEEK1)
        self.net.no_fundamentals = True                                   # HTTP 200, but nothing inside
        self.go(WEEK2)
        status = self.status()
        self.assertEqual(self.source("f")["status"], "ok")                # no transport errors...
        self.assertEqual(status["overall"], "degraded")                   # ...but the drop in coverage is caught
        self.assertTrue(any("EPS (actual)" in a for a in status["coverage_alerts"]))

    def test_missing_exchange_rate_carries_last_week(self):
        self.go(WEEK1)
        self.net.fx_missing = {"EUR"}
        self.go(WEEK2)
        b = self.row(WEEK2, "BBB")
        self.assertEqual(b["st"]["f"], "s:2026-11-13")
        self.assertAlmostEqual(b["eps_a"]["2025"], 5.72, places=2)
        status = self.status()
        self.assertEqual(status["fx"]["failed"], ["EUR"])
        self.assertEqual(status["overall"], "degraded")
        self.assertNotIn("f", self.row(WEEK2, "AAA").get("st", {}))

    def test_time_budget_exhaustion_is_resumable(self):
        self.go(WEEK1)
        with mock.patch.object(config, "TIME_BUDGET_S", -1):              # already out of time
            self.go(WEEK2)
        self.assertEqual(self.row(WEEK2, "AAA")["st"]["q"], "s:2026-11-13")
        self.assertIn("time budget", json.dumps(self.status()["sources"]))
        self.go(WEEK2)                                                    # next day's run finishes the job
        self.assertNotIn("st", self.row(WEEK2, "AAA"))
        self.assertEqual(self.status()["overall"], "ok")


class CrashTest(PipelineCase):
    def test_crash_is_reported_to_the_dashboard(self):
        self.go(WEEK1)
        with open(str(config.UNIVERSE_CSV), "w") as handle:
            handle.write("ticker\nAAA\n")                                 # someone broke the file
        with mock.patch("traceback.print_exc"):
            self.assertEqual(self.go(WEEK2), 1)
        status = self.status()
        self.assertEqual(status["overall"], "failed")
        self.assertIn("symbol", status["headline"])
        self.assertEqual(status["as_of"], WEEK1.isoformat())              # last good data is still what is published
        self.assertEqual(snapshot.read_json(config.INDEX_JSON)["latest"], WEEK1.isoformat())
        self.assertEqual(snapshot.read_json(config.RUNS_JSON)[0]["mode"], "crash")

    def test_bad_ticker_in_file_is_a_clear_error(self):
        with open(str(config.UNIVERSE_CSV), "a") as handle:
            handle.write("not a ticker!,x,,,\n")
        with mock.patch("traceback.print_exc"):
            self.assertEqual(self.go(WEEK1), 1)
        self.assertIn("not a valid ticker", self.status()["headline"])

    def test_one_broken_ticker_does_not_stop_the_rest(self):
        real = run.yahoo.fetch_quote

        def flaky(client, symbol, as_of):
            if symbol == "BBB":
                raise RuntimeError("boom")
            return real(client, symbol, as_of)

        with mock.patch("pipeline.run.yahoo.fetch_quote", flaky):
            self.assertEqual(self.go(WEEK1), 0)
        self.assertEqual(self.row(WEEK1, "BBB")["st"]["q"], "m")
        self.assertEqual(self.row(WEEK1, "AAA")["price"], 100.0)
        self.assertIn("internal error", json.dumps(self.source("q")["errors"]))


class UniverseChangeTest(PipelineCase):
    def test_added_and_removed_tickers(self):
        self.go(WEEK1)
        with open(str(config.UNIVERSE_CSV), "w") as handle:
            handle.write(UNIVERSE.replace("EEE,Epsilon Ltd,,,Widgets\n", "FFF,Phi plc,UK,,\n"))
        self.net.listed["FFF"] = ("United Kingdom", "Energy", "Oil", 5e9)
        self.go(WEEK1)                                                    # same week: only the new ticker is fetched
        symbols = [r["s"] for r in self.snap(WEEK1)["rows"]]
        self.assertEqual(symbols, ["AAA", "BBB", "CCCCY", "DDD", "FFF"])
        self.assertEqual(self.row(WEEK1, "FFF")["country"], "United Kingdom")
        self.assertEqual(self.net.count("/chart/FFF"), 1)
        self.assertEqual(self.net.count("/chart/AAA"), 1)


class OverridesTest(PipelineCase):
    def test_manual_values_are_attached_and_problems_reported(self):
        with open(str(config.OVERRIDES_CSV), "w") as handle:
            handle.write("symbol,field,value,note\n"
                         "AAA,eps_2026,6.2,GuruFocus 2026-11-16\n"
                         "AAA,z,5.61,\n"
                         "AAA,dy,2.5%,\n"
                         "ZZZ,z,1,\n"
                         "AAA,colour,blue,\n"
                         "AAA,f,seven,\n")
        self.go(WEEK1)
        a = self.row(WEEK1, "AAA")
        self.assertEqual(a["ovr"]["eps_2026"], [6.2, "GuruFocus 2026-11-16"])
        self.assertEqual(a["ovr"]["z"], [5.61, ""])
        self.assertAlmostEqual(a["ovr"]["dy"][0], 0.025)
        self.assertNotEqual(a["z"], 5.61)                                 # fetched value untouched underneath
        warnings = " ".join(self.status()["warnings"])
        self.assertIn("ZZZ is not in universe.csv", warnings)
        self.assertIn("unknown field 'colour'", warnings)
        self.assertIn("'seven' is not a number", warnings)

    def test_removing_an_override_restores_the_fetched_value(self):
        with open(str(config.OVERRIDES_CSV), "w") as handle:
            handle.write("symbol,field,value,note\nAAA,z,5.61,\n")
        self.go(WEEK1)
        os.remove(str(config.OVERRIDES_CSV))
        self.go(WEEK1, "--force")
        self.assertNotIn("ovr", self.row(WEEK1, "AAA"))


class HealthRulesTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(health.classify(0.0), "ok")
        self.assertEqual(health.classify(0.02), "ok")
        self.assertEqual(health.classify(0.03), "degraded")
        self.assertEqual(health.classify(0.5), "degraded")
        self.assertEqual(health.classify(0.51), "down")

    def test_coverage_alerts(self):
        alerts = health.coverage_alerts({"price": 0.99, "ev": 0.40}, {"price": 0.99, "ev": 0.85})
        self.assertEqual(len(alerts), 1)
        self.assertIn("Enterprise value", alerts[0])
        self.assertEqual(health.coverage_alerts({"price": 0.99}, None), [])

    def test_profile_refresh_is_staggered(self):
        base = {"s": "X", "ysector": "Tech"}
        due = [snapshot.profile_due("SYM%d" % i, WEEK2, base, WEEK1.isoformat()) for i in range(400)]
        share = sum(due) / 400.0
        self.assertTrue(0.15 < share < 0.35, share)                       # about a quarter each week
        self.assertTrue(snapshot.profile_due("X", WEEK2, None, None))
        old = dict(base, st={"p": "c:2026-09-25"})
        self.assertTrue(snapshot.profile_due("X", WEEK2, old, WEEK1.isoformat()))   # never older than five weeks


if __name__ == "__main__":
    unittest.main()
