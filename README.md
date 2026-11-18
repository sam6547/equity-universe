# Equity Universe dashboard

A small website that shows every ticker in the *Equity Universe* workbook with the
workbook's columns filled in, refreshes itself every trading day before the open, keeps past days so
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
| "Last FY adj." EPS | The latest reported year on the analysts' adjusted basis (worked out from Nasdaq's forecast growth figures) - the like-for-like starting point for the estimates. Exchange-listed stocks only |
| "Next FY" EPS | Yahoo's next-year consensus, recovered from price ÷ forward P/E (covers many OTC tickers too) |
| Market cap, Enterprise value | Yahoo Finance valuation measures |
| EBITDA, Free cash flow | Yahoo Finance statements, trailing twelve months |
| Dividend yield, 5-year dividend growth | Calculated from Yahoo's dividend history. Where that history has holes (many Japanese ADRs), yield comes from the cash-flow statement instead: dividends paid ÷ market value |
| Altman Z, Piotroski F | **Calculated here** from Yahoo's statements (formulas in `pipeline/compute.py`). Where a company reports no separate retained-earnings line, Z uses total reserves |
| CAGR of S&P 500 | Yahoo Finance index history (FRED as backup) |
| 10-year Treasury yield | U.S. Treasury (FRED as backup) |
| Inflation (CPI) | Bureau of Labor Statistics (FRED as backup) |

Analysts' figures (the estimates and "Last FY adj.") are on each company's adjusted basis; for
REITs that means funds from operations rather than earnings.

Companies that report in another currency (about half the list) are converted into
the currency the stock trades in; the ticker's detail panel shows the original figures.

### What cannot be automated, and why

GuruFocus, MarketWatch and Yahoo's profile / key-statistics pages only serve people using
a browser. GuruFocus sits behind Cloudflare bot protection, MarketWatch behind DataDome,
and Yahoo hands its data token only to browser sessions. **The job does not try to get past
those protections** - no disguised browsers, no automated page-scraping of protected sites.
It uses only feeds that answer an honestly identified script, and reports a refusal instead
of working around it.

What that costs, column by column (the dashboard's *Data health* tab carries the same list):

| Wanted | Shown instead | The gap |
|---|---|---|
| GuruFocus "EPS without NRI", past years | GAAP diluted EPS per year, plus the latest year on the analysts' adjusted basis (listed stocks) | GAAP includes one-off items; no adjusted figure for OTC tickers or for older years |
| EPS estimates to 2030, every ticker | Nasdaq consensus, 3-4 years, listed stocks; one implied forward year for most OTC tickers | No per-year estimates for OTC ADRs (about 45% of the list); fifth year usually blank |
| GuruFocus Z-Score / F-Score | Calculated from statements | Small differences; Z blank for banks and insurers; F blank where a test cannot be calculated |
| Yahoo levered free cash flow, Yahoo EBITDA | Free cash flow (operating cash flow − capex), EBITDA excluding unusual items | Same filings, different definitions: a few percent apart |
| Country from Yahoo's profile | Workbook label, else Nasdaq's listing record | Only as good as those two |
| Full dividend history | Cash-flow-statement yield where the history has holes | 5-year growth is out of date or blank for those tickers |

**Legitimate ways to close the gaps**

* **GuruFocus Data API** (paid, official). One request per ticker returns EPS without NRI,
  Altman Z and Piotroski F. GuruFocus members get a monthly allowance (2,000 requests on
  Premium, 20,000 on Premium Plus, as published in November 2026); a full pass over this
  list is about 950 requests. Its licence covers private use - feeding a *public* page
  needs their redistribution rights. A source module for it would slot into
  `pipeline/sources/` next to the others.
* **GuruFocus Excel add-in** (Premium members): pulls the same figures straight into the workbook.
* **A licensed market-data API** for analyst estimates on OTC ADRs.
* **Hand entry**: upload a filled-in sheet to the `manual/` folder (see *Add figures of your
  own* below); those figures win over the fetched ones and are marked ✎.

## How the daily refresh works

The job reports the **most recent trading day's close**. A day counts as final from 6 pm
New York time, so the main run is that evening and the dashboard is normally up to date
by about 7 pm. GitHub starts scheduled runs on a best-effort basis (at busy times late, or
not at all: on 18 Nov 2026 it skipped all three morning runs), so there are eight runs a
day; whichever gets there first does the refresh and the rest only check in (about 30 seconds each).
Times are UTC, so New York times shift by an hour with daylight saving:

| UTC   | New York (winter / summer) | What it does |
|-------|----------------------------|--------------|
| 23:37 | 6:37 pm / 7:37 pm          | main run: full refresh, about 15 minutes, ~3,000 polite requests |
| 01:47 | 8:47 pm / 9:47 pm          | retries anything that failed, otherwise just checks in |
| 04:07 | 11:07 pm / 12:07 am        | retry |
| 07:27 | 2:27 am / 3:27 am          | retry |
| 10:17 | 5:17 am / 6:17 am          | retry |
| 11:17 | 6:17 am / 7:17 am          | retry |
| 12:17 | 7:17 am / 8:17 am          | retry |
| 13:07 | 8:07 am / 9:07 am          | last retry, before the 9:30 open |

If every run is skipped, the dashboard says so from 9 am (winter) / 10 am (summer) New York time, and
*Manage tickers → Run the update now* (or GitHub → Actions → *Refresh data* → *Run workflow*)
fetches the day straight away.

* Weekends and NYSE holidays are skipped (the job knows the exchange calendar).
* A day counts as final from 6 pm New York time, so a run after that reports that day.
* Prices are always the as-of day's close: a retry that happens once the market is open
  still reports yesterday's close, never an intraday price.
* **History:** every trading day is kept for 90 days; older days are thinned to one per
  week (the week's last trading day), kept indefinitely. Pick any of them on the dashboard.

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

**Add or remove tickers on the dashboard** - press **Manage tickers**. The first time,
it asks for a GitHub access token (a pre-filled link creates one; pick this repository
under "Repository access"). After that:

* *Add*: type the symbol (as Yahoo Finance shows it) and optionally the company and
  country. It is saved to `universe.csv`, which starts the job; only the new ticker is
  fetched and it appears in about 3-5 minutes. If Yahoo does not recognise the symbol,
  the panel says so.
* *Remove*: open the ticker's detail panel → **Remove from the list**. It disappears at
  once, stops being fetched, and stays in earlier days. Removed tickers are kept in
  `universe.csv` with `hidden` = 1, so **Restore** brings them back with their labels.
* *Run the update now*: retries immediately instead of waiting for the next scheduled run.

The token stays in that browser (only for the tab session unless "Remember" is ticked)
and is sent only to GitHub. Anyone with it can change this repository, so keep it private;
revoke it at GitHub → Settings → Developer settings → Personal access tokens. A second
person (Dwight) needs his own GitHub account added as a collaborator, plus his own token:
GitHub's fine-grained tokens only reach repositories owned by the token's owner, so for a
collaborator either use a classic token with the `public_repo` scope, or move the
repository into a free GitHub organization you both belong to.

**Change the ticker list from the workbook** - edit the workbook, then on your computer:

```bash
python3 -m pipeline.import_universe "Equity Universe Version 2.xlsx"
```

Commit the updated `universe.csv`. New tickers are fetched on the next run. Tickers
removed on the dashboard stay removed; tickers added on the dashboard but missing from the
workbook are dropped (the command lists them). (This one step
needs `pip install openpyxl`; nothing else does.) You can also edit `universe.csv` directly.

**Add figures of your own (upload a sheet)**

Anything the job cannot fetch - GuruFocus's EPS without NRI, its Z and F scores, estimates for
OTC tickers - can be supplied by hand:

1. On the dashboard, narrow the table to the rows you want (the *Any row* menu has
   "No EPS estimates", "No Altman Z" and so on) and press **Blank sheet to fill in**.
   You get a CSV with those tickers and empty columns.
2. Fill in what you have - from a GuruFocus or Schwab export, or by reading the pages.
   Leave everything else empty. Set the *As of* date to when the figures were taken and,
   if you like, *Source* (for example `GuruFocus`).
3. In the repository on GitHub, open the `manual` folder → *Add file* → *Upload files* →
   drop the CSV in → *Commit changes*.

The dashboard updates a few minutes later. Rules:

* A filled cell wins over the fetched value and is marked ✎; an empty cell changes nothing.
* Column names are matched loosely, so a vendor's export usually works as it is, provided
  it has a `Symbol` (or `Ticker`) column. Headings that are not recognised are listed on
  the *Data health* tab rather than silently dropped.
* Numbers can be written as `1,234.5`, `$521.41B`, `(3.2)`, `2.5%` or `0.025`.
* Hand-entered figures do not refresh themselves. The *Data health* tab shows each file's
  date and flags it after 35 days; upload a newer file (same name) to replace it, or delete
  the file to go back to the fetched figures.
* Several files can sit side by side (`gurufocus.csv`, `schwab.csv`); if two give the same
  figure, the file later in alphabetical order wins. Files starting with `_` are ignored.
* The repository is public, so whatever you upload is public too. Data vendors' terms
  generally do not allow republishing their figures in bulk.

For a single figure it can be quicker to add one line to `overrides.csv`, which is read last:

```csv
symbol,field,value,note
SNY,z,2.33,GuruFocus Altman Z - read 2026-11-16
```

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
pipeline/run.py            the daily refresh (plan → fetch → assemble → health report)
pipeline/sources/          one module per source: yahoo.py, nasdaq.py, macro.py
pipeline/compute.py        dividend maths, Altman Z, Piotroski F, currency conversion
pipeline/net.py            polite HTTP client: pacing, retries, back-off, circuit breaker
pipeline/snapshot.py       daily files and retention, carry-forward of last good values, overrides
pipeline/manual.py         reads the sheets of hand-entered figures in manual/
pipeline/health.py         what the Data health tab shows
pipeline/report.py         GitHub issue + pass/fail for the workflow
site/                      the dashboard (plain HTML/CSS/JS) and its data
tests/                     154 tests, including simulated outages
.github/workflows/         the schedule
```

## A note on the data

The Yahoo and Nasdaq feeds used here are the public JSON endpoints behind their own
websites. They are free and need no key, but they are unofficial: they can change or be
withdrawn without notice, and their terms are written for personal use. The job identifies
itself honestly, paces its requests, and backs off when refused. Treat the figures as a
research aid and check anything important against a primary source - the dashboard marks
values it is unsure about with **?** and links each ticker to its source pages.
