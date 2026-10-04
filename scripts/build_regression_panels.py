#!/usr/bin/env python3
"""Build and install the regression-battery row on the Ops dashboard.

    python3 scripts/build_regression_panels.py            # print the JSON
    python3 scripts/build_regression_panels.py --install  # write it to Grafana

Four panels plus a state timeline, laid out as one row:

  * **Regression Battery** -- the glanceable one. A single 1/0 that is 1 only when
    the battery ran, ran recently, and everything passed. Mapped to the words OK
    and FAILING, because a bare 1/0 that a dashboard has to decode is worse than
    no number at all.
  * **Checks Passed / Run / Failed** -- the tally.
  * **Per-check status** -- which check is red, without clicking anything.
  * **Check history (30d)** -- the strip. Shows when a regression started and how
    long it lasted, which is the question a screenshot cannot answer.

Two things that are deliberate and worth stating, because both have been got
wrong in this project before:

  * **The headline is a product of three gauges, not one.** `failed == 0` alone
    would read green when the battery has stopped entirely, which is the exact
    failure this row exists to make visible. So it ANDs in freshness and the
    results-file error.
  * **The queries are generated, not typed by hand into the UI.** The metric names
    come from `audio_receiver.py`, so a rename breaks this script loudly instead
    of leaving a panel quietly blank forever. That is the same reasoning as
    tests/test_grafana_contract.py, and it is why this is a script and not a
    screenshot of a dashboard.

Install is idempotent: it reads the current dashboard, replaces any row carrying
this script's marker, appends the new one, and writes back with the version it
read so a concurrent edit is refused rather than clobbered.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_UID = "bb-ops-private"
DEFAULT_URL = "https://kevinwatkins.grafana.net"
PROM_UID = "grafanacloud-prom"

#: Stamped on the row so a re-run replaces it instead of appending a second copy.
MARKER = "battlebuddy-regression-battery-v1"
REGRESSION_ROW_TITLE = "Regression Battery — hourly, read-only, against production"

#: ops_verify's own staleness budget, so the panel and the gate cannot disagree.
#: Changing one without the other is exactly how the SLO page said "13 gates".
FRESH_SECONDS = 2 * 3600

_METRIC = re.compile(r"\b(battlebuddy_[a-z_0-9]+)\b")


def app_metric_names() -> set[str]:
    """Metric names taken from the source, so a rename fails here.

    Not a substitute for the runtime contract test -- this is a build-time check
    that the panel's queries name something real, so a typo cannot be installed.
    """
    src = (ROOT / "audio_receiver.py").read_text(encoding="utf-8")
    return set(_METRIC.findall(src))


def token() -> str:
    env = os.environ.get("BB_GRAFANA_TOKEN")
    if env:
        return env.strip()
    path = Path(os.path.expanduser(
        "~/.local/share/battlebuddy-homicide-watch/.grafana_token"))
    if not path.exists():
        sys.exit(f"no token: set BB_GRAFANA_TOKEN or create {path}")
    return path.read_text(encoding="utf-8").strip()


def api(path: str, tok: str, base: str, payload: dict | None = None) -> dict:
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        sys.exit(f"HTTP {exc.code} for {path}: {body}")
    except Exception as exc:
        sys.exit(f"could not reach {url}: {exc}")


def _ds() -> dict:
    return {"type": "prometheus", "uid": PROM_UID}


def _target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    target = {"datasource": _ds(), "expr": expr, "refId": ref}
    if legend:
        target["legendFormat"] = legend
    if instant:
        target["instant"] = True
        target["range"] = False
    return target


def _stat(title, description, expr, grid, steps, unit="short", decimals=0,
          mappings=None, legend=""):
    defaults = {
        "color": {"mode": "thresholds"},
        "decimals": decimals,
        "mappings": mappings or [],
        "thresholds": {"mode": "absolute", "steps": steps},
        "unit": unit,
    }
    return {
        "datasource": _ds(),
        "description": description,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": grid,
        "options": {
            "colorMode": "background",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
        },
        "pluginVersion": "",
        "targets": [_target(expr, legend=legend)],
        "title": title,
        "type": "stat",
    }


def build_row(y: int) -> dict:
    """The row. `y` is where it starts; laid out to be readable at a glance."""
    fresh = FRESH_SECONDS

    headline = (
        # 1 only when all three hold. Written as a product of comparisons rather
        # than three panels so that "stopped" and "failing" read the same way.
        f"(battlebuddy_regression_failed == bool 0)"
        f" * (battlebuddy_regression_last_run_age_seconds < {fresh})"
        f" * (battlebuddy_regression_error == bool 0)"
    )

    return {
        "collapsed": False,
        "description": (
            f"[{MARKER}] Hourly read-only regression battery "
            "(scripts/prod_regression.py, run by "
            "bb-prod-regression.timer on kevcloud). Reads nothing, writes nothing. "
            "Generated by scripts/build_regression_panels.py -- edit that, not the "
            "UI, or the next install will overwrite this row. ops_verify gates on "
            "the same three conditions."
        ),
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "id": 900,
        "panels": [
            _stat(
                "Regression Battery",
                "1 only when the battery ran, ran recently, and every check passed. "
                "Failing here means one of three things: a check failed, the timer "
                "stopped, or the results file is unreadable -- the headline cannot "
                "tell you which, the panels to the right can.",
                headline,
                {"h": 4, "w": 5, "x": 0, "y": y + 1},
                [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                mappings=[{
                    "type": "value",
                    "options": {
                        "0": {"color": "red", "index": 0, "text": "FAILING"},
                        "1": {"color": "green", "index": 1, "text": "OK"},
                    },
                }],
            ),
            _stat(
                "Checks Passed",
                "How many checks passed in the last run.",
                "sum(battlebuddy_regression_check)",
                {"h": 4, "w": 4, "x": 5, "y": y + 1},
                [{"color": "text", "value": None}],
            ),
            _stat(
                "Checks Run",
                "How many checks executed. A drop means the battery is finding less "
                "to check, which is worth noticing.",
                "battlebuddy_regression_ran",
                {"h": 4, "w": 3, "x": 9, "y": y + 1},
                [{"color": "text", "value": None}],
            ),
            _stat(
                "Checks Failed",
                "Zero means green. Anything above zero is a regression the battery "
                "has already caught.",
                "battlebuddy_regression_failed",
                {"h": 4, "w": 4, "x": 12, "y": y + 1},
                [{"color": "green", "value": None}, {"color": "red", "value": 1}],
            ),
            _stat(
                "Battery Age (min)",
                f"Minutes since the battery last wrote results. Amber at 75, red at "
                f"{fresh // 60}, matching the ops_verify staleness gate -- a battery "
                "that stops is the failure this panel exists to catch.",
                "battlebuddy_regression_last_run_age_seconds / 60",
                {"h": 4, "w": 4, "x": 16, "y": y + 1},
                [{"color": "green", "value": None},
                 {"color": "yellow", "value": int(fresh // 60 * 0.625)},
                 {"color": "red", "value": fresh // 60}],
                decimals=1,
            ),
            _stat(
                "Results Readable",
                "1 when the battery's results file could be read. 0 means the "
                "collector has nothing to report and every other panel in this row "
                "is showing its zero value rather than a measurement.",
                "battlebuddy_regression_error",
                {"h": 4, "w": 3, "x": 20, "y": y + 1},
                [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                mappings=[{
                    "type": "value",
                    "options": {
                        "0": {"color": "red", "index": 0, "text": "UNREADABLE"},
                        "1": {"color": "green", "index": 1, "text": "READABLE"},
                    },
                }],
            ),
            {
                "datasource": _ds(),
                "description":
                    "Which check is red, right now, without clicking anything. Each "
                    "bar is one check; a bar at zero is a regression.",
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "mappings": [],
                        "max": 1,
                        "min": 0,
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None},
                                      {"color": "green", "value": 1}],
                        },
                        "unit": "short",
                    },
                    "overrides": [],
                },
                "gridPos": {"h": 6, "w": 12, "x": 0, "y": y + 5},
                "options": {
                    "displayMode": "gradient",
                    "maxVizHeight": 300,
                    "minVizHeight": 16,
                    "minVizWidth": 8,
                    "namePlacement": "left",
                    "orientation": "horizontal",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "showUnfilled": True,
                    "sizing": "auto",
                    "valueMode": "text",
                },
                "pluginVersion": "",
                "targets": [_target("battlebuddy_regression_check",
                                    legend="{{check}}", ref="A")],
                "title": "Per-check status",
                "type": "bargauge",
            },
            {
                "datasource": _ds(),
                "description":
                    "The strip. One row per check, 30 days. Answers the question a "
                    "screenshot cannot: when did this start, and how long did it "
                    "last. Grafana defaults to 6h, so widen the time range to see it.",
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "custom": {
                            "fillOpacity": 70,
                            "lineWidth": 0,
                            "spanNulls": False,
                        },
                        "mappings": [],
                        "max": 1,
                        "min": 0,
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None},
                                      {"color": "green", "value": 1}],
                        },
                    },
                    "overrides": [],
                },
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": y + 5},
                "options": {
                    "alignValue": "left",
                    "legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True},
                    "mergeValues": True,
                    "rowHeight": 0.85,
                    "showValue": "never",
                    "tooltip": {"mode": "single", "sort": "none"},
                },
                "pluginVersion": "",
                "targets": [_target("battlebuddy_regression_check",
                                    legend="{{check}}", ref="A")],
                "title": "Check history (30d)",
                "type": "state-timeline",
            },
        ],
        "title": REGRESSION_ROW_TITLE,
        "type": "row",
    }


def _next_y(dashboard: dict) -> int:
    """Below everything already on the board, so nothing is displaced."""
    def walk(panels):
        for p in panels or []:
            g = p.get("gridPos") or {}
            yield g.get("y", 0) + g.get("h", 1)
            yield from walk(p.get("panels"))
    return max(walk(dashboard.get("panels")), default=0) + 1


def _strip_existing(dashboard: dict) -> int:
    """Remove any row this script previously installed."""
    removed = 0

    def clean(panels):
        nonlocal removed
        out = []
        for p in panels or []:
            # Two ways in, because a row this script installed must be
            # replaceable even if the marker was added after it was written.
            if (p.get("description") and MARKER in p["description"]) or \
                    (p.get("type") == "row" and REGRESSION_ROW_TITLE in (p.get("title") or "")):
                removed += 1
                continue
            p["panels"] = clean(p.get("panels"))
            out.append(p)
        return out

    dashboard["panels"] = clean(dashboard.get("panels"))
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true",
                    help="write the row to Grafana (default: print only)")
    ap.add_argument("--base", default=DEFAULT_URL)
    ap.add_argument("--uid", default=DASHBOARD_UID)
    args = ap.parse_args(argv)

    # Build-time check: every metric the queries name must exist in the source.
    named = set(_METRIC.findall(json.dumps(build_row(0))))
    known = app_metric_names()
    unknown = sorted(named - known)
    if unknown:
        sys.exit(f"these panel queries name metrics the app does not define: "
                 f"{unknown}. The row was not installed.")

    if not args.install:
        print(json.dumps(build_row(_next_y({"panels": []})), indent=1))
        return 0

    tok = token()
    current = api(f"/api/dashboards/uid/{args.uid}", tok, args.base)
    dashboard = current["dashboard"]
    version = current.get("meta", {}).get("version")
    removed = _strip_existing(dashboard)
    row = build_row(_next_y(dashboard))
    dashboard.setdefault("panels", []).append(row)

    result = api("/api/dashboards/db", tok, args.base, {
        "dashboard": dashboard,
        "folderUid": current["meta"].get("folderUid"),
        "message": "Add the regression-battery row (generated)",
        "overwrite": False,
    })
    print(f"installed on {args.uid}"
          + (f" (replaced {removed} existing row)" if removed else "")
          + f" -> version {result.get('version')} (was {version})")
    print("Now run: python3 scripts/fetch_grafana_dashboards.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())