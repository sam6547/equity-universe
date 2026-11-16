"""Central configuration. Everything tunable lives here."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Inputs
UNIVERSE_CSV = ROOT / "universe.csv"      # the ticker list (generated from the workbook)
OVERRIDES_CSV = ROOT / "overrides.csv"    # optional manual values that win over fetched ones

# Outputs (everything the dashboard reads is a static file under site/data)
SITE_DIR = ROOT / "site"
DATA_DIR = SITE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
INDEX_JSON = DATA_DIR / "index.json"
STATUS_JSON = DATA_DIR / "status.json"
RUNS_JSON = DATA_DIR / "runs.json"
BUILD_DIR = ROOT / "build"                # scratch output for CI (not published)

SCHEMA_VERSION = 1

# The client identifies itself honestly and never pretends to be a browser.
# If a source refuses this client, the refusal is reported on the dashboard
# instead of being worked around.
USER_AGENT = os.environ.get("DASHBOARD_USER_AGENT", "equity-universe-dashboard/1.0")

HTTP_TIMEOUT = 25           # seconds per request
WORKERS = 6                 # symbols processed concurrently (per-host pacing still applies)
TIME_BUDGET_S = int(os.environ.get("DASHBOARD_TIME_BUDGET_S", 150 * 60))  # stop starting new work after this

# Per-host pacing: minimum seconds between request starts. Hosts not listed use DEFAULT.
HOST_INTERVALS = {
    "yahoo": 0.40,
    "nasdaq": 0.50,
    "DEFAULT": 0.50,
}
# Hostname -> pacing bucket (query1/query2 share one budget).
HOST_BUCKETS = {
    "query1.finance.yahoo.com": "yahoo",
    "query2.finance.yahoo.com": "yahoo",
    "api.nasdaq.com": "nasdaq",
}

# A "blocked" answer (HTTP 429/403/401) pauses the whole host, doubling each time.
BLOCK_PAUSE_BASE_S = 20
BLOCK_PAUSE_MAX_S = 300
BLOCK_MAX_STRIKES = 5        # consecutive pauses without a success -> give up on the host for this run
ERROR_MAX_CONSECUTIVE = 15   # consecutive network/5xx failures -> give up on the host for this run

# Sector/industry change rarely: refresh each ticker's profile every N weeks (staggered).
PROFILE_REFRESH_WEEKS = 4

# How many years of history to request for prices/dividends, and the EPS columns shown.
EPS_YEARS_BACK = 3           # actual fiscal years shown before the as-of year
EPS_YEARS_FORWARD = 4        # estimate years shown after the as-of year

# Which S&P 500 growth figure fills the workbook's single "CAGR of S&P 500" column.
SP500_HEADLINE = {"series": "price", "years": 10}
SP500_HORIZONS = (1, 3, 5, 10, 20, 30)

# Health thresholds (share of applicable tickers whose fetch failed).
DEGRADED_ABOVE = 0.02
DOWN_ABOVE = 0.50
COVERAGE_DROP_ALERT = 0.10   # a field losing >10 points of coverage week-over-week is flagged

RUN_LOG_KEEP = 120           # runs kept in runs.json


def set_data_dir(path):
    """Point all outputs at another folder (used for trial runs and tests)."""
    global DATA_DIR, SNAPSHOT_DIR, INDEX_JSON, STATUS_JSON, RUNS_JSON
    DATA_DIR = Path(path)
    SNAPSHOT_DIR = DATA_DIR / "snapshots"
    INDEX_JSON = DATA_DIR / "index.json"
    STATUS_JSON = DATA_DIR / "status.json"
    RUNS_JSON = DATA_DIR / "runs.json"
