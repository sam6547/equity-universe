import os
import shutil
import tempfile
import unittest

from pipeline import manual

KNOWN = ["A", "AAPL", "BF/B", "SNY", "TM"] + ["T%03d" % i for i in range(40)]


class HeaderTest(unittest.TestCase):
    def test_workbook_and_export_headings(self):
        cases = {
            "Symbol": "symbol", "Ticker": "symbol", "As of": "asof", "Week Ending": "asof", "Source": "source",
            "2023 EPS": "eps_2023", "2026 EPS (estimate)": "eps_2026", "EPS 2027": "eps_2027", "EPS without NRI 2025": "eps_2025",
            "Next FY EPS (implied)": "feps", "Market Capitalization": "mcap", "Market Cap": "mcap", "Enterprise Value": "ev",
            "EBITDA": "ebitda", "Leveraged Free Cash Flow": "fcf", "Free Cash Flow": "fcf", "Leveraged Free Cashflow": "fcf",
            "Exchange": "exch", "Dividend Yield": "dy", "Dividend Yield %": "dy", "5 Year Dividend Growth Rate": "dg5",
            "Altman Z-Score": "z", "Altman Z- score": "z", "Piotroski F-Score": "f", "Piotroski F Value": "f",
            "Price": "price", "Country": "country", "Sector": "sector", "Industry": "industry",
        }
        for heading, field in cases.items():
            self.assertEqual(manual.field_for_header(heading), field, heading)

    def test_headings_that_are_skipped_quietly_or_flagged(self):
        for heading in ("Company Name", "Notes", "CAGR of S&P 500", "10 Yr Treasury Yield", "Inflation (CPI)", "Last FY EPS (adjusted)", ""):
            self.assertEqual(manual.field_for_header(heading), "", heading)
        for heading in ("Colour", "EPS", "P/E", "Beta"):
            self.assertIsNone(manual.field_for_header(heading), heading)


class ValueTest(unittest.TestCase):
    def test_numbers_in_the_shapes_exports_use(self):
        self.assertEqual(manual.parse_value("eps_2025", "4.89"), 4.89)
        self.assertEqual(manual.parse_value("eps_2025", "(1.75)"), -1.75)
        self.assertEqual(manual.parse_value("eps_2025", "-1.75"), -1.75)
        self.assertEqual(manual.parse_value("price", "$1,495.16"), 1495.16)
        self.assertEqual(manual.parse_value("mcap", "$521.41B (Large Cap)"), 521.41e9)
        self.assertEqual(manual.parse_value("mcap", "33.75 billion"), 33.75e9)
        self.assertEqual(manual.parse_value("ebitda", "850M"), 850e6)
        self.assertEqual(manual.parse_value("ev", "1.2T"), 1.2e12)
        self.assertEqual(manual.parse_value("f", "7"), 7)
        self.assertEqual(manual.parse_value("z", "2.33"), 2.33)

    def test_percentages_however_they_are_written(self):
        for text in ("2.5%", "2.5", "0.025", "2.50 %"):
            self.assertAlmostEqual(manual.parse_value("dy", text), 0.025, msg=text)
        self.assertAlmostEqual(manual.parse_value("dg5", "-3.2%"), -0.032)
        self.assertAlmostEqual(manual.parse_value("dg5", "7.1"), 0.071)

    def test_empty_cells_and_text(self):
        for text in ("", "  ", "-", "N/A", "n/a", "#N/A", "—"):
            self.assertIsNone(manual.parse_value("z", text), repr(text))
        self.assertEqual(manual.parse_value("country", "  United   Kingdom "), "United Kingdom")

    def test_unreadable_cells_raise(self):
        for field, text in (("z", "abc"), ("f", "12"), ("f", "6.5"), ("eps_2025", "see note")):
            with self.assertRaises(ValueError):
                manual.parse_value(field, text)

    def test_dates(self):
        self.assertEqual(manual.parse_date("2026-11-16"), "2026-11-16")
        self.assertEqual(manual.parse_date("11/16/2026"), "2026-11-16")
        self.assertEqual(manual.parse_date("Nov 16, 2026"), "2026-11-16")
        self.assertIsNone(manual.parse_date("last week"))


class FileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash-manual-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_a_spreadsheet_style_file(self):
        path = self.write("gurufocus.csv", "﻿Symbol,Company Name,As of,Source,2025 EPS,Altman Z-Score,Dividend Yield\n"
                                           "A,Agilent,11/16/2026,GuruFocus,4.89,5.61,0.59%\n"
                                           "bf.b,Brown-Forman,2026-11-16,,1.84,,\n"
                                           "SNY,Sanofi,,,,2.33,\n")
        entries, problems, summary = manual.read_file(path, KNOWN, label="manual/gurufocus.csv")
        self.assertEqual(problems, [])
        got = {(s, f): (v, n) for s, f, v, n in entries}
        self.assertEqual(got[("A", "eps_2025")], (4.89, "GuruFocus · as of 2026-11-16"))
        self.assertAlmostEqual(got[("A", "dy")][0], 0.0059)
        self.assertEqual(got[("BF/B", "eps_2025")], (1.84, "manual/gurufocus.csv · as of 2026-11-16"))   # BF.B is the workbook's BF/B
        self.assertEqual(got[("SNY", "z")], (2.33, "manual/gurufocus.csv"))
        self.assertEqual(len(entries), 5)
        self.assertEqual((summary["values"], summary["tickers"], summary["as_of"], summary["undated"]), (5, 3, "2026-11-16", 1))
        self.assertEqual(summary["fields"], ["dy", "eps_2025", "z"])

    def test_a_date_in_the_file_name_dates_every_row(self):
        path = self.write("schwab-2026-11-14.csv", "Ticker,Dividend Yield\nTM,3.1\n")
        entries, _problems, summary = manual.read_file(path, KNOWN)
        self.assertEqual(entries[0][3], "schwab-2026-11-14.csv · as of 2026-11-14")
        self.assertEqual(summary["as_of"], "2026-11-14")

    def test_later_rows_win_and_comment_rows_are_skipped(self):
        path = self.write("x.csv", "Symbol,Altman Z-Score\nA,1.0\n#A,9.9\nA,2.0\n")
        entries, _problems, _summary = manual.read_file(path, KNOWN)
        self.assertEqual([(s, f, v) for s, f, v, _n in entries], [("A", "z", 2.0)])

    def test_files_that_cannot_be_used(self):
        self.assertIn("no 'Symbol' column", manual.read_file(self.write("a.csv", "Name,Altman Z-Score\nApple,1\n"), KNOWN)[1][0])
        self.assertIn("empty", manual.read_file(self.write("b.csv", "\n\n"), KNOWN)[1][0])
        self.assertIn("no columns of figures", manual.read_file(self.write("c.csv", "Symbol,Company Name\nA,Agilent\n"), KNOWN)[1][0])

    def test_uploading_the_whole_export_is_called_out(self):
        heads = ["Symbol", "Price", "2023 EPS", "2024 EPS", "2025 EPS", "2026 EPS", "Market Cap", "Enterprise Value", "EBITDA",
                 "Free Cash Flow", "Dividend Yield", "Altman Z-Score", "Piotroski F-Score"]
        lines = [",".join(heads)] + ["%s,10,1,1,1,1,5B,6B,1B,1B,2%%,3.0,6" % s for s in KNOWN]
        _entries, problems, _summary = manual.read_file(self.write("export.csv", "\n".join(lines) + "\n"), KNOWN)
        self.assertTrue(any("no longer" in p and "refreshed" in p for p in problems), problems)

    def test_folder_reading_order_and_ignored_files(self):
        self.write("1-first.csv", "Symbol,Altman Z-Score\nA,1.0\n")
        self.write("2-second.csv", "Symbol,Altman Z-Score\nA,2.0\n")
        self.write("_template.csv", "Symbol,Altman Z-Score\nA,9.9\n")
        self.write("notes.txt", "not a csv")
        entries, problems, summaries = manual.load_folder(KNOWN, self.tmp)
        self.assertEqual([s["file"] for s in summaries], ["manual/1-first.csv", "manual/2-second.csv"])
        self.assertEqual(manual.desired_overrides(entries)["A"]["z"][0], 2.0)        # the later file wins
        self.assertEqual(manual.load_folder(KNOWN, os.path.join(self.tmp, "missing")), ([], [], []))


if __name__ == "__main__":
    unittest.main()
