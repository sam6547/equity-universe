import os
import shutil
import tempfile
import unittest
from unittest import mock

from pipeline import config, report, snapshot


def run(mode, overall):
    return {"mode": mode, "overall": overall}


class AttentionRuleTest(unittest.TestCase):
    def test_failure_always_needs_attention(self):
        self.assertTrue(report.needs_attention({"overall": "failed"}, []))

    def test_healthy_never_does(self):
        self.assertFalse(report.needs_attention({"overall": "ok"}, [run("full", "degraded")] * 5))
        self.assertFalse(report.needs_attention({}, []))
        self.assertFalse(report.needs_attention(None, None))

    def test_one_incomplete_run_is_left_for_the_retry(self):
        self.assertFalse(report.needs_attention({"overall": "degraded"}, [run("full", "degraded"), run("full", "ok"), run("full", "ok")]))

    def test_three_incomplete_runs_in_a_row_raise_it(self):
        runs = [run("retry", "degraded"), run("heartbeat", "degraded"), run("retry", "degraded"), run("full", "degraded")]
        self.assertTrue(report.needs_attention({"overall": "degraded"}, runs))     # check-ins do not count as attempts
        runs[3] = run("full", "ok")
        self.assertFalse(report.needs_attention({"overall": "degraded"}, runs))


class IssueTextTest(unittest.TestCase):
    STATUS = {
        "overall": "failed", "as_of": "2026-11-20", "last_check": "2026-11-21T11:40:00Z",
        "headline": "Prices & dividends - Yahoo Finance (chart data): 947 of 947 tickers not refreshed (HTTP 429: request refused) | S&P 500 growth not refreshed",
        "sources": [{"label": "Prices & dividends", "source": "Yahoo Finance (chart data)", "status": "down",
                     "counts": {"fresh": 0, "cached": 0, "stale": 947, "missing": 0, "nodata": 1}, "fix": "Check finance.yahoo.com is up."}],
        "coverage_alerts": ["Price is filled in for 3% of tickers, down from 100% last week"],
        "links": {"run": "https://github.com/o/r/actions/runs/1"},
    }

    def test_body_says_what_failed_and_how_to_fix_it(self):
        body = report.issue_body(self.STATUS)
        self.assertIn("**Status:** failed", body)
        self.assertIn("- S&P 500 growth not refreshed", body)
        self.assertIn("| Prices & dividends | Yahoo Finance (chart data) | down | 0 | 947 | 0 |", body)
        self.assertIn("**Prices & dividends:** Check finance.yahoo.com is up.", body)
        self.assertIn("Coverage alerts", body)
        self.assertIn("https://github.com/o/r/actions/runs/1", body)

    def test_body_survives_an_almost_empty_status(self):
        self.assertIn("No detail recorded.", report.issue_body({}))

    def test_commit_message(self):
        self.assertEqual(report.commit_message({"as_of": "2026-11-20", "overall": "ok"}, [run("full", "ok")]),
                         "refresh for week ending 2026-11-20 (ok)")
        self.assertEqual(report.commit_message({"as_of": "2026-11-20", "overall": "ok"}, [run("heartbeat", "ok"), run("full", "ok")]),
                         "check-in for week ending 2026-11-20 (ok)")
        self.assertEqual(report.commit_message({}, []), "no status recorded")


class RecordFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash-report-")
        self.saved = {k: getattr(config, k) for k in ("DATA_DIR", "SNAPSHOT_DIR", "INDEX_JSON", "STATUS_JSON", "RUNS_JSON")}
        config.set_data_dir(self.tmp)

    def tearDown(self):
        for key, value in self.saved.items():
            setattr(config, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failure_outside_the_fetch_reaches_the_dashboard(self):
        snapshot.write_json(config.STATUS_JSON, {"overall": "ok", "as_of": "2026-11-13", "last_check": "2026-11-14T11:30:00Z", "mode": "full"})
        report.record_failure("unit tests failed")
        status = snapshot.read_json(config.STATUS_JSON)
        self.assertEqual(status["overall"], "failed")
        self.assertIn("unit tests failed", status["headline"])
        self.assertEqual(status["as_of"], "2026-11-13")            # still describes the data that is published
        self.assertEqual(snapshot.read_json(config.RUNS_JSON)[0]["mode"], "crash")

    def test_does_not_overwrite_a_fresh_crash_report_from_the_fetch(self):
        from pipeline import dates
        snapshot.write_json(config.STATUS_JSON, {"overall": "failed", "mode": "crash", "last_check": dates.iso_ts(),
                                                 "headline": "The refresh job crashed: ValueError: universe.csv needs a 'symbol' column"})
        report.record_failure("a step failed")
        self.assertIn("universe.csv", snapshot.read_json(config.STATUS_JSON)["headline"])
        self.assertIsNone(snapshot.read_json(config.RUNS_JSON))

    def test_works_with_no_status_file_at_all(self):
        report.record_failure("checkout failed")
        self.assertEqual(snapshot.read_json(config.STATUS_JSON)["overall"], "failed")

    def test_issue_sync_never_raises_without_the_github_cli(self):
        with mock.patch("pipeline.report.subprocess.run", side_effect=OSError("gh not installed")), mock.patch("builtins.print"):
            report.sync_issue({"overall": "failed"}, [])               # must not raise

    def test_exit_code_reflects_the_refresh_not_the_issue_tracker(self):
        snapshot.write_json(config.STATUS_JSON, {"overall": "failed", "headline": "x"})
        with mock.patch("pipeline.report.sync_issue"), mock.patch("builtins.print"):
            self.assertEqual(report.main(["--issue"]), 1)
        snapshot.write_json(config.STATUS_JSON, {"overall": "degraded", "headline": "x"})
        with mock.patch("pipeline.report.sync_issue"), mock.patch("builtins.print"):
            self.assertEqual(report.main(["--issue"]), 0)


class OverridesFileTest(unittest.TestCase):
    def test_comment_and_blank_lines_are_ignored(self):
        tmp = tempfile.mkdtemp(prefix="dash-ovr-")
        try:
            path = os.path.join(tmp, "overrides.csv")
            with open(path, "w") as handle:
                handle.write("symbol,field,value,note\n# a comment, with commas\n\n#A,z,1,\nA,z,5.61,GuruFocus\nBF/B,country,United States,\n")
            rows, problems = snapshot.load_overrides(path)
            self.assertEqual(problems, [])
            self.assertEqual(rows, [("A", "z", 5.61, "GuruFocus"), ("BF/B", "country", "United States", "")])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shipped_template_is_valid_and_empty(self):
        rows, problems = snapshot.load_overrides(config.ROOT / "overrides.csv")
        self.assertEqual((rows, problems), ([], []))


if __name__ == "__main__":
    unittest.main()
