"""Tells people (not just the dashboard) when the refresh needs attention.

    python -m pipeline.report                    # print a summary of the last run
    python -m pipeline.report --commit-message   # one line for the data commit
    python -m pipeline.report --record-failure "what happened"   # the job died before/outside the fetch step
    python -m pipeline.report --issue            # open / update / close the GitHub issue; exit 1 if the refresh failed
    python -m pipeline.report --validate-site    # exit 1 unless every published data file is intact

Used by the scheduled workflow. It only reads the JSON the run wrote, so it keeps
working even when the fetch code itself is broken.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

from . import config, dates, snapshot

LABEL = "data-refresh"
TITLE = "Daily data refresh needs attention"
_REAL_RUNS = ("full", "retry", "crash")


def needs_attention(status, runs):
    """A person should look when the refresh failed outright, or has been incomplete three runs in a row.

    One incomplete run is normal - tomorrow's retry usually fixes it - so it does not raise an issue.
    """
    overall = (status or {}).get("overall")
    if overall == "failed":
        return True
    if overall != "degraded":
        return False
    recent = [r for r in (runs or []) if r.get("mode") in _REAL_RUNS][:3]
    return len(recent) == 3 and all(r.get("overall") != "ok" for r in recent)


def issue_body(status):
    links = status.get("links") or {}
    lines = [
        "The scheduled data refresh for the equity dashboard needs a look.",
        "",
        "**Status:** %s" % status.get("overall", "unknown"),
        "**Trading day:** %s" % status.get("as_of", "unknown"),
        "**Last run:** %s" % status.get("last_check", "unknown"),
        "",
        "**What happened**",
    ]
    lines += ["- %s" % part for part in str(status.get("headline") or "No detail recorded.").split(" | ")]
    sources = status.get("sources") or []
    if sources:
        lines += ["", "| Feed | Source | Status | Fresh | Carried over | Unavailable |", "|---|---|---|---:|---:|---:|"]
        for s in sources:
            c = s.get("counts") or {}
            lines.append("| %s | %s | %s | %d | %d | %d |" % (s.get("label"), s.get("source"), s.get("status"),
                                                           c.get("fresh", 0) + c.get("cached", 0), c.get("stale", 0), c.get("missing", 0)))
        fixes = [(s.get("label"), s.get("fix")) for s in sources if s.get("fix")]
        if fixes:
            lines += ["", "**How to fix**"] + ["- **%s:** %s" % f for f in fixes]
    for key, title in (("coverage_alerts", "Coverage alerts"), ("warnings", "Warnings")):
        if status.get(key):
            lines += ["", "**%s**" % title] + ["- %s" % item for item in status[key]]
    if status.get("error"):
        lines += ["", "**Crash:** `%s`" % status["error"]]
    if links.get("run") or links.get("actions"):
        lines += ["", "[Run log](%s) - the dashboard's *Data health* tab has the same detail." % (links.get("run") or links.get("actions"))]
    lines += ["", "_This issue is updated by the refresh job and closes itself once a run completes cleanly._"]
    return "\n".join(lines) + "\n"


def commit_message(status, runs):
    if not status:
        return "no status recorded"
    latest = (runs or [{}])[0] if isinstance(runs, list) and runs else {}
    kind = {"full": "refresh", "retry": "retry", "heartbeat": "check-in", "crash": "crash", "update": "update"}.get(latest.get("mode"), "update")
    return "%s for %s (%s)" % (kind, status.get("as_of", "?"), status.get("overall", "?"))


def record_failure(reason):
    """The workflow failed somewhere the fetch code could not record (tests, checkout, a crash before start-up)."""
    status = snapshot.read_json(config.STATUS_JSON, {}) or {}
    last = status.get("last_check")
    if status.get("mode") == "crash" and last:
        try:
            age = dates.utc_now() - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            if age < dt.timedelta(minutes=30):
                return  # the fetch step already wrote a more specific crash report
        except ValueError:
            pass
    stamp = dates.iso_ts()
    headline = "The refresh job failed before it could fetch data: %s" % reason
    links = _links() or status.get("links", {})
    status.update({"schema": config.SCHEMA_VERSION, "last_check": stamp, "overall": "failed", "headline": headline,
                   "error": reason, "mode": "crash", "links": links})
    snapshot.write_json(config.STATUS_JSON, status)
    runs = snapshot.read_json(config.RUNS_JSON, [])
    runs = runs if isinstance(runs, list) else []
    entry = {"ts": stamp, "as_of": status.get("as_of"), "mode": "crash", "overall": "failed", "headline": headline,
             "trigger": os.environ.get("GITHUB_EVENT_NAME") or "local", "run": links.get("run")}
    runs.insert(0, {k: v for k, v in entry.items() if v is not None})
    snapshot.write_json(config.RUNS_JSON, runs[:config.RUN_LOG_KEEP])


def validate_site():
    """Problems that must stop a publish: the dashboard is only as good as the files it is handed.

    Returns a list of messages; empty means every data file parses and has the shape the page expects.
    """
    problems = []

    def load(path, label):
        try:
            with open(str(path), encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            problems.append("%s cannot be read (%s)" % (label, exc.__class__.__name__))
            return None
        if "<<<<<<<" in text or ">>>>>>>" in text:
            problems.append("%s contains merge-conflict markers" % label)
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            problems.append("%s is not valid JSON (%s)" % (label, str(exc)[:80]))
            return None

    index = load(config.INDEX_JSON, "index.json")
    status = load(config.STATUS_JSON, "status.json")
    runs = load(config.RUNS_JSON, "runs.json")
    if status is not None and not isinstance(status, dict):
        problems.append("status.json is not an object")
    if runs is not None and not isinstance(runs, list):
        problems.append("runs.json is not a list")
    if index is not None:
        entries = index.get("snapshots") if isinstance(index, dict) else None
        if not isinstance(entries, list) or not entries:
            problems.append("index.json lists no snapshots")
        else:
            for entry in entries:
                name = "snapshots/%s.json" % entry.get("as_of")
                snap = load(config.SNAPSHOT_DIR / ("%s.json" % entry.get("as_of")), name)
                if snap is not None and not (isinstance(snap, dict) and isinstance(snap.get("rows"), list) and snap["rows"]):
                    problems.append("%s has no rows" % name)
    return problems


def _links():
    from . import health
    return health.repo_links()


def _gh(*args):
    """Run the GitHub CLI; returns (ok, stdout). Never raises."""
    try:
        done = subprocess.run(["gh"] + list(args), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr).strip()


def sync_issue(status, runs):
    """Open, update or close the tracking issue. Problems here are reported but never fail the job."""
    ok, out = _gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number", "--jq", ".[0].number // empty")
    if not ok:
        print("Could not query issues (%s) - skipping issue update." % out[:200])
        return
    number = out.strip()
    if needs_attention(status, runs):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(issue_body(status))
            path = handle.name
        try:
            if number:
                ok, out = _gh("issue", "edit", number, "--body-file", path)
                print("Updated issue #%s." % number if ok else "Could not update issue: %s" % out[:200])
            else:
                _gh("label", "create", LABEL, "--color", "D93F0B", "--description", "Raised by the data refresh job", "--force")
                ok, out = _gh("issue", "create", "--title", TITLE, "--label", LABEL, "--body-file", path)
                print("Opened issue: %s" % out if ok else "Could not open issue: %s" % out[:200])
        finally:
            os.unlink(path)
    elif number and (status or {}).get("overall") == "ok":
        ok, out = _gh("issue", "close", number, "--comment", "The refresh completed cleanly on %s. Closing." % status.get("last_check", "the latest run"))
        print("Closed issue #%s." % number if ok else "Could not close issue: %s" % out[:200])
    else:
        print("No issue change needed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit-message", action="store_true")
    parser.add_argument("--record-failure", metavar="REASON")
    parser.add_argument("--issue", action="store_true")
    parser.add_argument("--validate-site", action="store_true")
    args = parser.parse_args(argv)

    if args.validate_site:
        problems = validate_site()
        for problem in problems:
            print("::error::Data file check failed: %s" % problem)
        if not problems:
            print("Data files are intact.")
        return 1 if problems else 0

    if args.record_failure:
        record_failure(args.record_failure)
    status = snapshot.read_json(config.STATUS_JSON, {}) or {}
    runs = snapshot.read_json(config.RUNS_JSON, [])
    if args.commit_message:
        print(commit_message(status, runs))
        return 0
    print("Refresh status: %s - %s" % (str(status.get("overall", "unknown")).upper(), status.get("headline", "no run recorded")))
    if args.issue:
        sync_issue(status, runs)
        return 1 if status.get("overall") == "failed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
