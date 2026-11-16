# Equity Universe dashboard

A small website that shows every ticker in the *Equity Universe* workbook with the
workbook's columns filled in, refreshes itself once a week, keeps every past week so
you can look back, and says so on the page when a data source lets it down.

It is deliberately simple: a scheduled script writes JSON files, and a static page
reads them. There is no server, no database and nothing to install.

```
universe.csv ──▶ pipeline/ (Python, standard library only) ──▶ site/data/*.json ──▶ site/index.html
   tickers          runs on a schedule in GitHub Actions           one file per week      the dashboard
```

## What you see

| Workbook column | Where it comes from |
|---|---|
| Symbol, Company name, Price, Exchange | Yahoo Finance chart data (Friday's close for the week) |
| Sector, Industry | Yahoo Finance search; the workbook's own label if Yahoo has none |
| Country | The workbook's label; Nasdaq's listing where the workbook is blank |
| 2023 – 2025 EPS (reported) | Yahoo Finance income statements - GAAP diluted EPS |
| 2026 – 2030 EPS (estimates) | Nasdaq analyst forecasts - consensus for the next 3-4 fiscal years, **exchange-listed stocks only** |
| "Next FY" EPS | Yahoo's next-year consensus, recovered from price ÷ forward P/E (covers OTC tickers too) |
| Market cap, Enterprise value | Yahoo Finance valuation measures |
| EBITDA, Free cash flow | Yahoo Finance statements, trailing twelve months |
| Dividend yield, 5-year dividend growth | Calculated from Yahoo's dividend history |
| Altman Z, Piotroski F | **Calculated here** from Yahoo's statements (formulas in `pipeline/compute.py`) |
| CAGR of S&P 500 | Yahoo Finance index history (FRED as backup) |
| 10-year Treasury yield | U.S. Treasury (FRED as backup) |
| Inflation (CPI) | Bureau of Labor Statistics (FRED as backup) |

Companies that report in another currency (about half the list) are converted into
the currency the stock trades in; the ticker's detail panel shows the original figures.

### Sources in the workbook that cannot be automated

GuruFocus, MarketWatch and Yahoo's profile / key-statistics pages only serve people
using a browser - they refuse any script (bot protection, or a login token Yahoo will
not issue to scripts). The job does not try to get around that. In practice:

* **GuruFocus "EPS without NRI" and its 5-year estimates** → reported GAAP EPS from Yahoo, and Nasdaq's
  consensus estimates. These are *not the same numbers*: GAAP EPS includes one-off items, and Nasdaq
  has no estimates for OTC-traded ADRs.
* **GuruFocus Z-Score / F-Score** → calculated from the statements. Expect small differences; the Z-Score
  is blank where Yahoo has no retained-earnings figure and for banks and insurers.
* **Yahoo "levered free cash flow" and its S&P-calculated EBITDA** → free cash flow (operating cash flow −
  capex) and EBITDA excluding unusual items, from the same filings.

If you want a figure from one of those pages, type it into `overrides.csv` (see below).
GuruFocus also sells a data API; with a key, a new module under `pipeline/sources/` could
feed those columns properly.

## How the weekly refresh works

The job runs **every day** (GitHub Actions, 11:17 UTC):

1. It works out which week to report: the one that closed last Friday.
2. No snapshot for that week yet → **full refresh** (about 20 minutes, ~3,400 polite requests).
3. Snapshot exists but something failed → **retry only what failed**.
4. Snapshot complete → nothing to fetch; it just **checks in** so the page knows the job is alive.

So you get one refresh a week (normally Saturday morning US time) plus automatic retries
for the next six days.

### When a source goes down

* The value from the last good week **stays on screen**, marked ◷, with the week it came from.
* A banner appears at the top of the dashboard, and the **Data health** tab lists each feed,
  how many tickers it failed for, the error, and what to do about it.
* The job stops calling a source that keeps refusing it (after five back-offs) rather than hammering it.
* If the refresh fails outright, or is incomplete three runs in a row, the job opens a GitHub
  issue (and closes it again when things recover). GitHub also emails you when a run fails.
* If the **job itself stops running**, the page notices on its own: it warns when the job has not
  checked in for two days, or when a week's data is missing.
* A column that silently goes empty (a source changing its format without an error) is caught by
  the week-over-week coverage check.

## Everyday tasks

**Change the ticker list** - edit the workbook, then on your computer:

```bash
python3 -m pipeline.import_universe "Equity Universe Version 2.xlsx"
```

Commit the updated `universe.csv`. New tickers are fetched on the next run. (This one step
needs `pip install openpyxl`; nothing else does.) You can also edit `universe.csv` directly.

**Type in a figure by hand** - add a line to `overrides.csv`:

```csv
symbol,field,value,note
A,eps_2026,6.20,GuruFocus EPS without NRI - read 2026-11-16
SNY,z,2.33,GuruFocus Altman Z
```

Hand-typed values show with a ✎ mark. Delete the line and the fetched value comes back.

**Refresh right now** - GitHub → Actions → *Refresh data* → *Run workflow* (tick "force" to
re-fetch the whole week).

**Run it on your own computer**

```bash
python3 -m unittest discover -s tests -t .      # the tests (about 3 seconds, no network)
python3 -m pipeline.run --symbols A,SNY,AAPL    # trial run on a few tickers, writes to build/trial
python3 -m pipeline.run                         # the real thing, writes to site/data
python3 -m http.server 8765 --directory site    # then open http://localhost:8765
python3 -m tests.ui_fixture                     # builds build/ui-fixture: what an outage looks like
```

## Fixing things

| Symptom on the Data health tab | Likely cause | What to do |
|---|---|---|
| A Yahoo feed shows `HTTP 429` / `403` | Yahoo is rate-limiting or blocking the job's address | Wait for tomorrow's retry. If it persists, slow the job down (`HOST_INTERVALS` in `pipeline/config.py`) or run the job from another network (it runs fine from a home computer). |
| A feed shows a `format` error | The source changed the layout of its answers | Update the matching `parse_…` function in `pipeline/sources/`. Each has tests in `tests/test_sources.py`. |
| "Coverage fell from X% to Y%" | A source is answering with empty data | Same as above - open one of its URLs and compare with the parser. |
| "has not checked in for N days" | The schedule is paused (GitHub pauses schedules in inactive repositories) or Actions is disabled | GitHub → Actions → *Refresh data* → enable / *Run workflow*. |
| A ticker under "Needs attention" | Renamed, delisted, or the ADR ended | Fix the symbol in the workbook and rebuild `universe.csv`. |

Tunable settings (request pace, time budget, which S&P 500 growth figure is the headline,
how many EPS years to show) are all in `pipeline/config.py`.

## Layout

```
pipeline/run.py            the weekly refresh (plan → fetch → assemble → health report)
pipeline/sources/          one module per source: yahoo.py, nasdaq.py, macro.py
pipeline/compute.py        dividend maths, Altman Z, Piotroski F, currency conversion
pipeline/net.py            polite HTTP client: pacing, retries, back-off, circuit breaker
pipeline/snapshot.py       weekly files, carry-forward of last good values, overrides
pipeline/health.py         what the Data health tab shows
pipeline/report.py         GitHub issue + pass/fail for the workflow
site/                      the dashboard (plain HTML/CSS/JS) and its data
tests/                     116 tests, including simulated outages
.github/workflows/         the schedule
```

## A note on the data

The Yahoo and Nasdaq feeds used here are the public JSON endpoints behind their own
websites. They are free and need no key, but they are unofficial: they can change or be
withdrawn without notice, and their terms are written for personal use. The job identifies
itself honestly, paces its requests, and backs off when refused. Treat the figures as a
research aid and check anything important against a primary source - the dashboard marks
values it is unsure about with **?** and links each ticker to its source pages.
