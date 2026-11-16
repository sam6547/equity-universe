import datetime as dt
import unittest

from pipeline import compute, dates

D = dt.date


class NumbersTest(unittest.TestCase):
    def test_round_sig(self):
        self.assertEqual(compute.round_sig(123456789.0, 6), 123457000)
        self.assertEqual(compute.round_sig(0.000123456, 3), 0.000123)
        self.assertEqual(compute.round_sig(0), 0)
        self.assertIsNone(compute.round_sig(float("nan")))
        self.assertIsNone(compute.round_sig(None))

    def test_cagr(self):
        self.assertAlmostEqual(compute.cagr(100, 200, 10), 0.071773, places=5)
        self.assertIsNone(compute.cagr(0, 200, 10))
        self.assertIsNone(compute.cagr(100, -5, 10))
        self.assertIsNone(compute.cagr(100, 200, 0))

    def test_index_cagrs(self):
        # an index that doubles every 10 years, weekly points
        start = D(1996, 1, 5)
        pts = [(start + dt.timedelta(days=7 * i), 100 * 2 ** ((7 * i) / 3652.5)) for i in range(1620)]
        out = compute.index_cagrs(pts, D(2026, 11, 13), (1, 10, 30, 40))
        self.assertAlmostEqual(out["10"], 0.0718, places=3)
        self.assertAlmostEqual(out["1"], 0.0718, places=3)
        self.assertAlmostEqual(out["30"], 0.0718, places=3)
        self.assertNotIn("40", out)  # not enough history

    def test_index_cagrs_needs_recent_end(self):
        pts = [(D(2020, 1, 3), 100.0), (D(2025, 1, 3), 150.0)]
        self.assertEqual(compute.index_cagrs(pts, D(2026, 11, 13), (1, 5)), {})


class FxTest(unittest.TestCase):
    def setUp(self):
        self.fx = compute.FxBook("USD")
        self.fx.add("EUR", [(D(2025, 1, 3) + dt.timedelta(days=7 * i), 1.0 + 0.001 * i) for i in range(100)])

    def test_base_currency_is_one(self):
        self.assertEqual(self.fx.spot("USD", D(2026, 1, 1)), 1.0)
        self.assertEqual(self.fx.average("USD", D(2025, 1, 1), D(2026, 1, 1)), 1.0)

    def test_spot_uses_latest_at_or_before(self):
        self.assertAlmostEqual(self.fx.spot("EUR", D(2025, 1, 12)), 1.001)
        self.assertIsNone(self.fx.spot("EUR", D(2024, 12, 1)))
        self.assertIsNone(self.fx.spot("EUR", D(2028, 1, 1)))   # too old to trust
        self.assertIsNone(self.fx.spot("JPY", D(2025, 6, 1)))

    def test_average_needs_coverage(self):
        avg = self.fx.average("EUR", D(2025, 1, 3), D(2026, 1, 2))
        self.assertAlmostEqual(avg, 1.0265, places=3)
        self.assertIsNone(self.fx.average("EUR", D(2023, 1, 1), D(2025, 2, 1)))  # mostly before the series starts


class DividendTest(unittest.TestCase):
    AS_OF = D(2026, 11, 13)

    def quarterly(self, start_amount=0.20, growth=1.05, years=8, last=D(2026, 10, 6)):
        pays = []
        for q in range(years * 4):
            day = last - dt.timedelta(days=91 * q)
            pays.append((day, round(start_amount * growth ** (-(q // 4)), 6)))
        return sorted(pays)

    def test_quarterly_payer(self):
        out = compute.dividend_metrics(self.quarterly(), self.AS_OF, 100.0)
        self.assertEqual(out["status"], "paying")
        self.assertEqual(out["freq"], 4)
        self.assertAlmostEqual(out["ttm"], 0.80, places=6)
        self.assertAlmostEqual(out["yield"], 0.008, places=6)
        self.assertAlmostEqual(out["growth5"], 0.05, places=3)

    def test_annual_payer_whose_date_drifts(self):
        # Paid once a year, between 345 and 369 days apart, plus a one-off special in 2022.
        pays = [(D(2019, 5, 10), 1.50), (D(2020, 5, 4), 1.55), (D(2021, 5, 5), 1.60), (D(2022, 5, 4), 1.747),
                (D(2022, 5, 26), 0.297), (D(2023, 5, 30), 1.903), (D(2024, 5, 9), 2.037), (D(2025, 5, 9), 2.212),
                (D(2026, 5, 4), 2.422)]
        out = compute.dividend_metrics(pays, self.AS_OF, 48.37)
        self.assertEqual(out["freq"], 1)
        self.assertAlmostEqual(out["ttm"], 2.422)              # exactly one payment, never zero or two
        self.assertAlmostEqual(out["yield"], 2.422 / 48.37, places=5)
        self.assertAlmostEqual(out["growth5"], (2.422 / 1.60) ** (1 / 5.0) - 1, places=3)
        # a few days after this year's payment the answer must be the same
        again = compute.dividend_metrics(pays, D(2026, 5, 8), 48.37)
        self.assertAlmostEqual(again["ttm"], 2.422)

    def test_special_next_to_regular_is_counted_once(self):
        pays = [(D(2020, 5, 4), 1.55), (D(2021, 5, 5), 1.60), (D(2022, 5, 4), 1.747), (D(2022, 5, 26), 0.297)]
        out = compute.dividend_metrics(pays, D(2022, 6, 30), 50.0)
        self.assertAlmostEqual(out["ttm"], 1.747 + 0.297)

    def test_gap_in_source_data_gives_unknown_not_zero(self):
        # Semi-annual payer whose history simply stops 19 months ago.
        pays = [(D(2022, 3, 30), 2.2), (D(2022, 9, 29), 1.76), (D(2023, 3, 30), 2.5), (D(2023, 9, 28), 2.0),
                (D(2024, 3, 27), 2.86), (D(2024, 9, 30), 2.6), (D(2025, 3, 31), 3.46)]
        out = compute.dividend_metrics(pays, self.AS_OF, 200.0)
        self.assertEqual(out["status"], "gap")
        self.assertIsNone(out["yield"])
        self.assertIsNone(out["growth5"])

    def test_gap_with_long_history_still_reports_growth_up_to_the_last_payment(self):
        pays = self.quarterly(last=D(2025, 3, 31))
        out = compute.dividend_metrics(pays, self.AS_OF, 100.0)
        self.assertEqual(out["status"], "gap")
        self.assertIsNone(out["yield"])
        self.assertAlmostEqual(out["growth5"], 0.05, places=3)
        self.assertTrue(out["growth_stale"])
        self.assertFalse(compute.dividend_metrics(self.quarterly(), self.AS_OF, 100.0)["growth_stale"])

    def test_patchy_history_gives_no_growth_figure(self):
        # Only four payments on record over six years: two in the latest year, one five years earlier.
        pays = [(D(2020, 3, 30), 0.116), (D(2024, 3, 27), 0.127), (D(2024, 9, 30), 0.166), (D(2025, 3, 31), 0.269)]
        out = compute.dividend_metrics(pays, self.AS_OF, 21.83)
        self.assertEqual(out["status"], "gap")
        self.assertIsNone(out["growth5"])

    def test_long_stopped_payer_is_zero(self):
        out = compute.dividend_metrics([(D(2018, 9, 5), 1.0), (D(2019, 3, 5), 1.0), (D(2019, 9, 5), 1.0)], self.AS_OF, 80.0)
        self.assertEqual(out["status"], "none")
        self.assertEqual(out["yield"], 0.0)

    def test_never_paid(self):
        out = compute.dividend_metrics([], self.AS_OF, 80.0)
        self.assertEqual(out["yield"], 0.0)
        self.assertEqual(out["status"], "none")
        self.assertIsNone(compute.dividend_metrics([], self.AS_OF, None)["yield"])

    def test_recently_slowed_payer_reports_strict_window(self):
        pays = self.quarterly(last=D(2026, 4, 1))   # nothing for seven months
        out = compute.dividend_metrics(pays, self.AS_OF, 100.0)
        self.assertEqual(out["status"], "lapsed")
        self.assertAlmostEqual(out["ttm"], 0.40, places=6)   # only the two payments inside the last 365 days
        self.assertIsNone(out["growth5"])

    def test_future_payments_ignored(self):
        pays = self.quarterly() + [(D(2027, 1, 5), 99.0)]
        out = compute.dividend_metrics(pays, self.AS_OF, 100.0)
        self.assertAlmostEqual(out["ttm"], 0.80, places=6)

    def test_special_payment_is_marked_for_checking(self):
        pays = [(D(2022, 5, 2), 0.121), (D(2023, 5, 3), 0.13), (D(2024, 5, 1), 0.138), (D(2025, 5, 7), 0.155),
                (D(2026, 4, 28), 0.164), (D(2026, 6, 15), 1.683)]
        out = compute.dividend_metrics(pays, self.AS_OF, 10.69)
        self.assertTrue(out["check"])
        self.assertAlmostEqual(out["ttm"], 1.847)

    def test_holes_in_the_history_are_marked_for_checking(self):
        # Three payments a year until the big two simply stop appearing in the source.
        pays = [(D(2024, 1, 9), 0.874), (D(2024, 5, 9), 0.022), (D(2024, 7, 5), 1.518), (D(2025, 1, 13), 0.956),
                (D(2025, 5, 23), 0.023), (D(2025, 7, 7), 1.918), (D(2026, 5, 22), 0.024)]
        out = compute.dividend_metrics(pays, self.AS_OF, 104.44)
        self.assertTrue(out["check"])

    def test_absurd_yield_is_marked_for_checking(self):
        out = compute.dividend_metrics([(D(2026, 6, 29), 5.785)], self.AS_OF, 6.53)
        self.assertTrue(out["check"])

    def test_ordinary_payers_are_not_marked(self):
        self.assertFalse(compute.dividend_metrics(self.quarterly(), self.AS_OF, 100.0)["check"])
        self.assertFalse(compute.dividend_metrics([], self.AS_OF, 100.0)["check"])

    def test_short_history_has_no_growth(self):
        out = compute.dividend_metrics(self.quarterly(years=3), self.AS_OF, 100.0)
        self.assertIsNone(out["growth5"])
        self.assertAlmostEqual(out["ttm"], 0.80, places=6)


class AltmanTest(unittest.TestCase):
    def test_matches_hand_calculation(self):
        # Agilent, July 2026 balance sheet + trailing twelve months (USD millions)
        z, parts, missing = compute.altman_z(2404, 13967, 1912, 1764, 49044, 6604, 7372)
        self.assertEqual(missing, [])
        self.assertAlmostEqual(z, 5.799, places=2)
        self.assertEqual(len(parts), 5)
        self.assertEqual(compute.z_zone(z), "safe")

    def test_reports_what_is_missing(self):
        z, parts, missing = compute.altman_z(2404, 13967, None, 1764, 49044, 6604, 7372)
        self.assertIsNone(z)
        self.assertEqual(missing, ["retained earnings"])

    def test_rejects_nonpositive_denominators(self):
        self.assertIsNone(compute.altman_z(1, 0, 1, 1, 1, 1, 1)[0])
        self.assertIsNone(compute.altman_z(1, 10, 1, 1, 1, 0, 1)[0])

    def test_zones(self):
        self.assertEqual(compute.z_zone(1.5), "distress")
        self.assertEqual(compute.z_zone(2.0), "grey")
        self.assertIsNone(compute.z_zone(None))


class PiotroskiTest(unittest.TestCase):
    # Sanofi FY2023..FY2025 (EUR millions) - GuruFocus shows an F-Score of 7.
    SNY = [
        {"ni": 5400, "cfo": 10258, "ta": 126464, "ltd": 14347, "ca": 30670, "cl": 24207, "shares": 2503.4, "gp": 28990, "rev": 41618},
        {"ni": 5560, "cfo": 9081, "ta": 132798, "ltd": 11791, "ca": 42588, "cl": 29162, "shares": 2502.8, "gp": 31081, "rev": 44286},
        {"ni": 7813, "cfo": 10750, "ta": 126805, "ltd": 14248, "ca": 30952, "cl": 28345, "shares": 2440.8, "gp": 33667, "rev": 46716},
    ]

    def test_full_score(self):
        score, tests = compute.piotroski_f(self.SNY)
        self.assertEqual(tests, [1, 1, 1, 1, 0, 0, 1, 1, 1])
        self.assertEqual(score, 7)

    def test_missing_inputs_give_no_score_but_partial_tests(self):
        bank = [dict(y, ca=None, cl=None, gp=None) for y in self.SNY]
        score, tests = compute.piotroski_f(bank)
        self.assertIsNone(score)
        self.assertEqual(tests[5], None)
        self.assertEqual(tests[7], None)
        self.assertEqual(tests[0], 1)

    def test_needs_three_years(self):
        self.assertEqual(compute.piotroski_f(self.SNY[1:]), (None, [None] * 9))

    def test_debt_free_company_passes_leverage(self):
        years = [dict(y, ltd=0.0) for y in self.SNY]
        self.assertEqual(compute.piotroski_f(years)[1][4], 1)

    def test_dilution_fails(self):
        years = [dict(y) for y in self.SNY]
        years[2]["shares"] = 2600.0
        self.assertEqual(compute.piotroski_f(years)[1][6], 0)


class DatesTest(unittest.TestCase):
    def test_last_completed_friday(self):
        self.assertEqual(dates.last_completed_friday(D(2026, 11, 14)), D(2026, 11, 13))  # Saturday
        self.assertEqual(dates.last_completed_friday(D(2026, 11, 15)), D(2026, 11, 13))  # Sunday
        self.assertEqual(dates.last_completed_friday(D(2026, 11, 16)), D(2026, 11, 13))  # Monday
        self.assertEqual(dates.last_completed_friday(D(2026, 11, 19)), D(2026, 11, 13))  # Thursday
        self.assertEqual(dates.last_completed_friday(D(2026, 11, 20)), D(2026, 11, 13))  # Friday: week not finished
        self.assertEqual(dates.last_completed_friday(D(2026, 11, 21)), D(2026, 11, 20))
        self.assertEqual(dates.last_completed_friday(D(2027, 1, 2)), D(2027, 1, 1))      # across a year end

    def test_fiscal_label_year(self):
        self.assertEqual(dates.fiscal_label_year(D(2025, 10, 31)), 2025)
        self.assertEqual(dates.fiscal_label_year(D(2026, 1, 3)), 2025)   # 53-week year ending just after New Year
        self.assertEqual(dates.fiscal_label_year(D(2026, 1, 31)), 2026)  # January year-end is labelled by its own year
        self.assertEqual(dates.fiscal_label_year(D(2026, 3, 31)), 2026)

    def test_parse_month_year(self):
        self.assertEqual(dates.parse_month_year("Oct 2026"), D(2026, 10, 31))
        self.assertEqual(dates.parse_month_year("Feb 2028"), D(2028, 2, 29))
        self.assertIsNone(dates.parse_month_year("N/A"))
        self.assertIsNone(dates.parse_month_year(None))
        self.assertIsNone(dates.parse_month_year("Foo 2026"))

    def test_epoch_roundtrip(self):
        self.assertEqual(dates.from_epoch(dates.to_epoch(D(2026, 11, 13))), D(2026, 11, 13))
        self.assertEqual(dates.from_epoch(dates.to_epoch(D(2026, 11, 13)) + 3600, -18000), D(2026, 11, 12))


if __name__ == "__main__":
    unittest.main()
