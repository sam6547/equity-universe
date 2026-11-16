import datetime as dt
import unittest

from pipeline import compute, dates
from pipeline.sources import macro, nasdaq, yahoo

from . import fakes

D = dt.date
AS_OF = D(2026, 11, 13)


class ChartTest(unittest.TestCase):
    def fields(self, **kw):
        meta, bars, divs = yahoo.parse_chart(fakes.chart_payload(AS_OF, **kw))
        return yahoo.quote_fields(meta, bars, divs, AS_OF)

    def test_price_is_the_as_of_week_close_not_a_later_bar(self):
        out = self.fields(price=100.0)
        self.assertEqual(out["price"], 100.0)            # the following week's bar (300.0) is ignored
        self.assertEqual(out["pweek"], "2026-11-09")
        self.assertNotIn("plag", out)
        self.assertAlmostEqual(out["chg1w"], 100.0 / 99.95 - 1, places=4)

    def test_identity_fields(self):
        out = self.fields(exchange="NMS", name="Sanofi")
        self.assertEqual(out["exch"], "NASDAQ")
        self.assertEqual(out["yname"], "Sanofi")
        self.assertEqual(out["cur"], "USD")
        self.assertEqual(self.fields(exchange="PNK")["exch"], "OTC")
        self.assertEqual(self.fields(exchange="OID")["exch"], "OTC")
        self.assertEqual(self.fields(exchange="TEX")["exch"], "TXSE")
        meta, bars, divs = yahoo.parse_chart(fakes.chart_payload(AS_OF, exchange="ZZZ"))
        meta["fullExchangeName"] = "OTC Markets Something New"
        self.assertEqual(yahoo.quote_fields(meta, bars, divs, AS_OF)["exch"], "OTC")

    def test_range_and_one_year_return(self):
        out = self.fields(price=100.0)
        self.assertAlmostEqual(out["hi52"], 102.0, places=3)
        self.assertAlmostEqual(out["lo52"], round(100 * (1 - 0.0005 * 51) * 0.98, 4), places=3)
        self.assertAlmostEqual(out["ret1y"], 100.0 / (100 * (1 - 0.0005 * 52)) - 1, places=4)

    def test_dividends(self):
        out = self.fields(price=100.0, dividend=0.25)
        self.assertAlmostEqual(out["div_ttm"], 1.0)
        self.assertAlmostEqual(out["dy"], 0.01)
        self.assertAlmostEqual(out["dg5"], 0.05, places=3)
        self.assertEqual(out["div_freq"], 4)

    def test_non_payer(self):
        out = self.fields(dividend=0)
        self.assertEqual(out["dy"], 0.0)
        self.assertNotIn("dg5", out)

    def test_source_gap_leaves_yield_blank(self):
        out = self.fields(dividend=0.25, last_div=D(2025, 3, 31))
        self.assertNotIn("dy", out)
        self.assertEqual(out["div_gap"], 1)
        self.assertEqual(out["div_last"], "2025-03-31")

    def test_stale_price_is_flagged(self):
        payload = fakes.chart_payload(AS_OF)
        res = payload["chart"]["result"][0]
        quote = res["indicators"]["quote"][0]
        for key in ("close", "high", "low"):
            quote[key][-2] = None    # no trade in the as-of week
            quote[key][-3] = None    # ... nor the week before
        meta, bars, divs = yahoo.parse_chart(payload)
        out = yahoo.quote_fields(meta, bars, divs, AS_OF)
        self.assertEqual(out["plag"], 2)
        self.assertEqual(out["pweek"], "2026-10-26")

    def test_live_point_is_merged_into_its_week(self):
        payload = fakes.chart_payload(AS_OF)
        res = payload["chart"]["result"][0]
        friday_close = dates.to_epoch(AS_OF) + 21 * 3600   # Friday 16:00 New York
        res["timestamp"].insert(-1, friday_close)
        for key, value in (("close", 101.5), ("high", 101.5), ("low", 101.5)):
            res["indicators"]["quote"][0][key].insert(-1, value)
        meta, bars, divs = yahoo.parse_chart(payload)
        out = yahoo.quote_fields(meta, bars, divs, AS_OF)
        self.assertEqual(out["price"], 101.5)
        self.assertAlmostEqual(out["chg1w"], 101.5 / 99.95 - 1, places=4)   # still compared with the prior week

    def test_unusable_payloads_raise(self):
        with self.assertRaises(ValueError):
            yahoo.parse_chart({"chart": {"result": None, "error": {"description": "No data found"}}})
        with self.assertRaises(ValueError):
            yahoo.parse_chart({"chart": {"result": [{"meta": {}, "timestamp": [], "indicators": {"quote": [{}]}}]}})
        with self.assertRaises(ValueError):
            yahoo.parse_chart({})

    def test_symbol_forms(self):
        self.assertEqual(yahoo.yahoo_symbol("BF/B"), "BF-B")
        self.assertEqual(yahoo.yahoo_symbol(" mog/a "), "MOG-A")
        self.assertEqual(nasdaq.forecast_symbol("BF/B"), "BF.B")
        self.assertEqual(yahoo.split_currency("GBp"), ("GBP", 0.01))
        self.assertEqual(yahoo.split_currency("EUR"), ("EUR", 1.0))
        self.assertEqual(yahoo.split_currency(None), (None, 1.0))


class FundamentalsTest(unittest.TestCase):
    def series(self, **kw):
        return yahoo.parse_timeseries(fakes.timeseries_payload(fakes.company_series(AS_OF, **kw)))

    def test_usd_company(self):
        out = yahoo.fundamentals_fields(self.series(), AS_OF, price=100.0)
        self.assertEqual(out["eps_a"], {"2022": 4.0, "2023": 4.4, "2024": 4.8, "2025": 5.2})
        self.assertNotIn("fcur", out)
        self.assertEqual(out["mcap"], 2500)
        self.assertEqual(out["ev"], 2800)
        self.assertEqual(out["ebitda"], 270)                 # normalized, trailing twelve months
        self.assertTrue(out["ebitda_b"].startswith("TTM "))
        self.assertEqual(out["fcf"], 120)
        self.assertEqual(out["fpe"], 16.0)
        self.assertAlmostEqual(out["feps"], 6.25)
        self.assertNotIn("unit_flag", out)                   # 100 / 5.4 = 18.5, matching Yahoo's own P/E
        z, _, _ = compute.altman_z(380, 2200, 720, 200, 2500, 910, 1150)
        self.assertAlmostEqual(out["z"], z, places=3)
        self.assertEqual(out["f"], 9)
        self.assertEqual(out["ft"], "111111111")

    def test_foreign_reporter_is_converted(self):
        fx = compute.FxBook("USD")
        fx.add("EUR", [(AS_OF - dt.timedelta(days=7 * i), 1.10) for i in range(330)])
        out = yahoo.fundamentals_fields(self.series(currency="EUR"), AS_OF, price=100.0, fx=fx)
        self.assertEqual(out["fcur"], "EUR")
        self.assertAlmostEqual(out["fx"], 1.10)
        self.assertAlmostEqual(out["eps_a"]["2025"], 5.72)   # 5.2 EUR at the year's average rate
        self.assertEqual(out["eps_ar"]["2025"], 5.2)
        self.assertAlmostEqual(out["ebitda"], 297.0)
        self.assertEqual(out["mcap"], 2500)                  # already quoted in USD
        z, _, _ = compute.altman_z(380, 2200, 720, 200, 2500 / 1.10, 910, 1150)
        self.assertAlmostEqual(out["z"], z, places=3)        # market value moved into EUR, not statements into USD

    def test_missing_exchange_rate_leaves_money_blank_rather_than_wrong(self):
        out = yahoo.fundamentals_fields(self.series(currency="EUR"), AS_OF, price=100.0, fx=compute.FxBook("USD"))
        self.assertEqual(out["fx_missing"], 1)
        self.assertNotIn("eps_a", out)
        self.assertNotIn("ebitda", out)
        self.assertNotIn("z", out)

    def test_z_score_reports_missing_inputs(self):
        out = yahoo.fundamentals_fields(self.series(with_retained=False), AS_OF, price=100.0)
        self.assertNotIn("z", out)
        self.assertEqual(out["z_miss"], ["retained earnings"])
        self.assertEqual(out["f"], 9)

    def test_unit_mismatch_is_flagged(self):
        out = yahoo.fundamentals_fields(self.series(), AS_OF, price=400.0)   # price implies P/E 74 vs Yahoo's 18.5
        self.assertEqual(out["unit_flag"], 1)

    def test_stale_trailing_figures_fall_back_to_last_fiscal_year(self):
        raw = fakes.company_series(AS_OF)
        old = (AS_OF - dt.timedelta(days=400)).isoformat()
        for key in ("trailingNormalizedEBITDA", "trailingEBITDA", "trailingFreeCashFlow"):
            raw[key] = [(old, raw[key][0][1], "USD")]
        out = yahoo.fundamentals_fields(yahoo.parse_timeseries(fakes.timeseries_payload(raw)), AS_OF, price=100.0)
        self.assertEqual(out["ebitda"], 260)
        self.assertEqual(out["ebitda_b"], "FY 2025-12-31")
        self.assertEqual(out["fcf"], 115)

    def test_stray_interim_rows_are_not_fiscal_years(self):
        raw = fakes.company_series(AS_OF, fy_month_end="03-31")
        raw["annualDilutedEPS"] = [("2025-03-31", 159.47, "USD"), ("2025-06-30", 159.52, "USD"),
                                   ("2025-09-30", 164.67, "USD"), ("2026-03-31", 212.33, "USD")]
        series = yahoo.parse_timeseries(fakes.timeseries_payload(raw))
        self.assertEqual([d.isoformat() for d in yahoo.fiscal_year_ends(series)],
                         ["2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31"])

    def test_eps_falls_back_to_income_over_shares(self):
        raw = fakes.company_series(AS_OF)
        raw["annualDilutedEPS"] = raw["annualDilutedEPS"][:3]     # latest year missing
        out = yahoo.fundamentals_fields(yahoo.parse_timeseries(fakes.timeseries_payload(raw)), AS_OF, price=100.0)
        self.assertAlmostEqual(out["eps_a"]["2025"], round(130 / 24.8, 4), places=3)

    def test_year_end_just_after_new_year_keeps_its_label(self):
        ends = [D(2023, 12, 30), D(2024, 12, 28), D(2026, 1, 3)]
        self.assertEqual([dates.fiscal_label_year(d) for d in ends], [2023, 2024, 2025])

    def test_layout_change_raises(self):
        with self.assertRaises(ValueError):
            yahoo.parse_timeseries({"finance": {"error": "nope"}})
        self.assertEqual(yahoo.parse_timeseries({"timeseries": {"result": []}}), {})


class SearchTest(unittest.TestCase):
    def test_exact_symbol_match(self):
        out = yahoo.parse_search(fakes.search_payload("SNY", "Healthcare", "Drug Manufacturers—General"), "SNY")
        self.assertEqual(out, {"ysector": "Healthcare", "yindustry": "Drug Manufacturers - General"})

    def test_class_share_symbol(self):
        out = yahoo.parse_search(fakes.search_payload("BF-B"), "BF/B")
        self.assertEqual(out["ysector"], "Industrials")

    def test_no_match(self):
        self.assertIsNone(yahoo.parse_search({"quotes": [{"symbol": "OTHER", "sector": "X"}]}, "SNY"))
        with self.assertRaises(ValueError):
            yahoo.parse_search({"nope": 1}, "SNY")


class NasdaqTest(unittest.TestCase):
    def test_forecast_rows(self):
        out = nasdaq.parse_forecast(fakes.forecast_payload(2026, month="Oct"))
        self.assertEqual(out, {"2026": [6.0, 8], "2027": [6.6, 6], "2028": [7.3, 4], "2029": [8.1, 2]})

    def test_unknown_symbol_is_no_data(self):
        self.assertIsNone(nasdaq.parse_forecast(fakes.FORECAST_UNKNOWN))

    def test_known_symbol_without_analyst_coverage_is_no_data(self):
        payload = {"data": {"symbol": "l", "quarterlyForecast": None, "yearlyForecast": None}, "status": {"rCode": 200}}
        self.assertIsNone(nasdaq.parse_forecast(payload))
        with self.assertRaises(ValueError):
            nasdaq.parse_forecast({"data": {"symbol": "l", "yearlyForecast": ["surprise"]}, "status": {"rCode": 200}})

    def test_text_numbers_and_blanks(self):
        payload = fakes.forecast_payload(2026)
        payload["data"]["yearlyForecast"]["rows"][0]["consensusEPSForecast"] = "$1,234.50"
        payload["data"]["yearlyForecast"]["rows"][1]["consensusEPSForecast"] = "N/A"
        out = nasdaq.parse_forecast(payload)
        self.assertEqual(out["2026"][0], 1234.5)
        self.assertNotIn("2027", out)

    def test_layout_change_raises(self):
        with self.assertRaises(ValueError):
            nasdaq.parse_forecast({"unexpected": True})
        with self.assertRaises(ValueError):
            nasdaq.parse_forecast({"status": {}, "data": {"somethingElse": 1}})

    def test_screener(self):
        listing = nasdaq.parse_screener(fakes.screener_payload({
            "BF/B": ("United States", "", "", ""), "LEN.B": ("United States", "Consumer Discretionary", "Homebuilding", 3e10),
            "ABC^A": ("United States", "", "", "")}))
        self.assertNotIn("ABC^A", listing)                       # preferred shares skipped
        self.assertEqual(nasdaq.screener_lookup(listing, "BF/B")["country"], "United States")
        self.assertEqual(nasdaq.screener_lookup(listing, "LEN/B")["mcap"], 3e10)
        self.assertIsNone(nasdaq.screener_lookup(listing, "MOG/A"))

    def test_truncated_screener_is_rejected(self):
        with self.assertRaises(ValueError):
            nasdaq.parse_screener({"data": {"rows": [{"symbol": "A", "country": "United States"}]}})


class MacroTest(unittest.TestCase):
    def test_treasury_csv(self):
        points = macro.parse_treasury_csv(fakes.treasury_csv(AS_OF, 5.15))
        self.assertEqual(points[-1], (AS_OF, 5.15))
        with self.assertRaises(ValueError):
            macro.parse_treasury_csv("<html>maintenance</html>")

    def test_cpi_year_over_year(self):
        monthly = {(2025, 10): 300.0, (2026, 9): 308.0, (2026, 10): 309.0}
        out = macro.cpi_from_monthly(monthly, AS_OF)
        self.assertAlmostEqual(out["yoy"], 0.03)
        self.assertEqual(out["month"], "2026-10")
        self.assertNotIn("note", out)

    def test_cpi_with_an_unpublished_base_month(self):
        monthly = {(2025, 9): 324.245, (2025, 11): 325.063, (2026, 10): 337.511}
        out = macro.cpi_from_monthly(monthly, AS_OF)
        self.assertAlmostEqual(out["yoy"], 337.511 / (324.245 * 325.063) ** 0.5 - 1, places=5)
        self.assertIn("never published", out["note"])

    def test_cpi_too_old_or_incomplete(self):
        self.assertIsNone(macro.cpi_from_monthly({(2025, 1): 300.0, (2026, 1): 309.0}, AS_OF))
        self.assertIsNone(macro.cpi_from_monthly({(2026, 10): 309.0}, AS_OF))
        self.assertIsNone(macro.cpi_from_monthly({}, AS_OF))


if __name__ == "__main__":
    unittest.main()
