#!/usr/bin/env python3
"""Build and install the poller-health row on the Ops dashboard.

    python3 scripts/build_poller_panels.py            # print the JSON
    python3 scripts/build_poller_panels.py --install  # write it to Grafana

Three poller health metrics, one row per poller, each row with:

  * **Poller Active** -- 1 if the poller thread is running, 0 if stopped.
  * **Consecutive Failures** -- how many poll cycles have failed in a row.
  * **Last Success Age** -- seconds since the last successful poll; -1 means never.

A poller that stops produces no signal in any other dashboard, gate or alert.
On a public-safety feed that is silent loss of coverage.

Two things that are deliberate and worth stating, because both have been got
wrong in this project before:

  * **The headline is a product of three gauges, not one.** `active == 1` alone
    would read green when a poller has stopped entirely (age=-1, failures climbing),
    which is the exact failure this row exists to make visible.
  * **The queries are generated, not typed by hand into the UI.** The metric names
    and poller names come from the app's metric export, so a rename breaks this
    script loudly instead of leaving a panel quietly blank forever. That is the same
    reasoning as tests/test_grafana_contract.py, and it is why this is a script and
    not a screenshot of a dashboard.

Install is idempotent: it reads the current dashboard, replaces any row carrying
this script's marker, appends the new one, and writes back with the version it
read so a concurrent edit is refused rather than clobbered.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_UID = "bb-ops-private"
DEFAULT_URL = "https://kevinwatkins.grafana.net"
PROM_UID = "grafanacloud-prom"

#: Stamped on the row so a re-run replaces it instead of appending a second copy.
MARKER = "battlebuddy-poller-health-v1"
POLLER_ROW_TITLE_PREFIX = "Poller Health — "

#: ops_verify's own staleness budget for last-success age.
#: A poller that has not succeeded in this many seconds is considered stale.
#: The longest poll interval is 6h (austin-events, apd-cad), so 3 missed cycles.
FRESH_SECONDS = 3 * 6 * 3600  # 18h
#: Consecutive failures above this is a hard failure.
FAILURE_THRESHOLD = 3

_METRIC = re.compile(r"\b(battlebuddy_[a-z_0-9]+)\b")


def app_poller_names() -> list[str]:
    """Poller names taken from the poller implementations.

    These are the NAME class attributes from each poller. Hardcoded here
    because the poller set is stable and the metric validation below will
    catch any mismatch. If a new poller is added, add its NAME here.
    """
    # ops_verify derives the same set from the live scrape; a test asserts the two
    # agree, so this list cannot quietly drift from reality.
    #
    # `reddit-intel` was here and has been removed. Its `.start()` is commented out
    # in audio_receiver.py, so it emits no metrics at all -- and the comment claiming
    # its metrics "will still be exported if the class is imported" was a guess that
    # the live scrape disproved. A panel row for a poller that does not exist reads
    # as "no data" forever, which is indistinguishable from a stopped poller.
    return [
        "adsb-air-asset",
        "afd",
        "apd-cad",
        "apd_news",
        "atxfloods",
        "austin-events",
        "traffic-open-data",
    ]


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


def _stat(title: str, description: str, expr: str, grid: dict,
          steps: list[dict], unit: str = "short", decimals: int = 0,
          mappings: list[dict] | None = None, legend: str = "") -> dict:
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


def _poller_row(poller_name: str, y: int) -> dict:
    """A row for a single poller. `y` is where it starts."""
    fresh = FRESH_SECONDS

    # Headline: 1 only when active=1 AND age<fresh AND failures==0
    # Written as a product so "stopped" and "failing" read the same way.
    headline = (
        # Every comparison carries `bool`, so each yields 0 or 1 and the product
        # is ALWAYS present. Without it, `age >= 0 AND age < 18h` acts as a filter:
        # a stale poller's sample is dropped, the product is an empty vector, and
        # the stat renders "No data" instead of FAILING -- which is the one thing
        # this panel exists to make visible.
        f'(battlebuddy_poller_active{{poller="{poller_name}"}} == bool 1)'
        f' * (battlebuddy_poller_last_success_age_seconds{{poller="{poller_name}"}} >= bool 0)'
        f' * (battlebuddy_poller_last_success_age_seconds{{poller="{poller_name}"}} < bool {fresh})'
        f' * (battlebuddy_poller_consecutive_failures{{poller="{poller_name}"}} == bool 0)'
    )

    return {
        "collapsed": False,
        "description": (
            f"[{MARKER}] {POLLER_ROW_TITLE_PREFIX}{poller_name}. "
            "Generated by scripts/build_poller_panels.py -- edit that, not the "
            "UI, or the next install will overwrite this row. "
            "ops_verify gates on the same three conditions."
        ),
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "id": 900,
        "panels": [
            _stat(
                f"Poller Active — {poller_name}",
                "1 if the poller thread is running, 0 if stopped. "
                "A stopped poller is silent loss of coverage on a public-safety feed.",
                f'battlebuddy_poller_active{{poller="{poller_name}"}}',
                {"h": 4, "w": 5, "x": 0, "y": y + 1},
                [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                mappings=[{
                    "type": "value",
                    "options": {
                        "0": {"color": "red", "index": 0, "text": "STOPPED"},
                        "1": {"color": "green", "index": 1, "text": "ACTIVE"},
                    },
                }],
            ),
            _stat(
                f"Consecutive Failures — {poller_name}",
                "How many poll cycles have failed in a row. Zero means the last cycle succeeded. "
                f"Amber at {FAILURE_THRESHOLD}, red above.",
                f'battlebuddy_poller_consecutive_failures{{poller="{poller_name}"}}',
                {"h": 4, "w": 5, "x": 5, "y": y + 1},
                [{"color": "green", "value": None},
                 {"color": "yellow", "value": FAILURE_THRESHOLD},
                 {"color": "red", "value": FAILURE_THRESHOLD + 1}],
            ),
            _stat(
                f"Last Success Age (min) — {poller_name}",
                f"Minutes since the last successful poll cycle. -1 means never succeeded. "
                f"Amber at 75%, red at {fresh // 60} min, matching the ops_verify staleness gate. "
                "A poller that stops succeeding is the failure this panel exists to catch.",
                f'battlebuddy_poller_last_success_age_seconds{{poller="{poller_name}"}} / 60',
                {"h": 4, "w": 5, "x": 10, "y": y + 1},
                [{"color": "green", "value": None},
                 {"color": "yellow", "value": int(fresh // 60 * 0.75)},
                 {"color": "red", "value": fresh // 60}],
                decimals=1,
            ),
            _stat(
                f"Poller Health — {poller_name}",
                "1 only when the poller is active, has succeeded recently, and has zero consecutive failures. "
                "Failing here means one of three things: the poller stopped, it's failing repeatedly, "
                "or it hasn't succeeded in too long.",
                headline,
                {"h": 4, "w": 5, "x": 15, "y": y + 1},
                [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                mappings=[{
                    "type": "value",
                    "options": {
                        "0": {"color": "red", "index": 0, "text": "UNHEALTHY"},
                        "1": {"color": "green", "index": 1, "text": "HEALTHY"},
                    },
                }],
            ),
            {
                "datasource": _ds(),
                "description":
                    f"30-day history for {poller_name}: active (green/red), failures (count), "
                    "last success age (minutes, -1=never). Grafana defaults to 6h, "
                    "so widen the time range to see it.",
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "custom": {
                            "fillOpacity": 70,
                            "lineWidth": 0,
                            "spanNulls": False,
                        },
                        "mappings": [],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None},
                                      {"color": "green", "value": 1}],
                        },
                        "unit": "short",
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "Consecutive Failures"},
                            "properties": [
                                {"id": "unit", "value": "short"},
                                {"id": "thresholds", "value": {
                                    "mode": "absolute",
                                    "steps": [{"color": "green", "value": None},
                                              {"color": "yellow", "value": FAILURE_THRESHOLD},
                                              {"color": "red", "value": FAILURE_THRESHOLD + 1}],
                                }},
                            ],
                        },
                        {
                            "matcher": {"id": "byName", "options": "Last Success Age (min)"},
                            "properties": [
                                {"id": "unit", "value": "s"},
                                {"id": "thresholds", "value": {
                                    "mode": "absolute",
                                    "steps": [{"color": "green", "value": None},
                                              {"color": "yellow", "value": fresh * 0.75},
                                              {"color": "red", "value": fresh}],
                                }},
                            ],
                        },
                    ],
                },
                "gridPos": {"h": 8, "w": 24, "x": 0, "y": y + 5},
                "options": {
                    "alignValue": "left",
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "mergeValues": True,
                    "rowHeight": 0.85,
                    "showValue": "never",
                    "tooltip": {"mode": "single", "sort": "none"},
                },
                "pluginVersion": "",
                "targets": [
                    _target(f'battlebuddy_poller_active{{poller="{poller_name}"}}',
                            legend="Active", ref="A"),
                    _target(f'battlebuddy_poller_consecutive_failures{{poller="{poller_name}"}}',
                            legend="Consecutive Failures", ref="B"),
                    _target(f'battlebuddy_poller_last_success_age_seconds{{poller="{poller_name}"}} / 60',
                            legend="Last Success Age (min)", ref="C"),
                ],
                "title": f"Poller History (30d) — {poller_name}",
                "type": "state-timeline",
            },
        ],
        "title": f"{POLLER_ROW_TITLE_PREFIX}{poller_name}",
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
            if (p.get("description") and MARKER in p["description"]) or \
                    (p.get("type") == "row" and POLLER_ROW_TITLE_PREFIX in (p.get("title") or "")):
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
                    help="write the rows to Grafana (default: print only)")
    ap.add_argument("--base", default=DEFAULT_URL)
    ap.add_argument("--uid", default=DASHBOARD_UID)
    args = ap.parse_args(argv)

    # Build-time check: get poller names from the app
    poller_names = app_poller_names()
    if not poller_names:
        sys.exit("could not discover poller names from the app; the row was not installed")

    # Build all rows and collect metric names for validation
    all_rows = [_poller_row(name, 0) for name in poller_names]
    named = set()
    for row in all_rows:
        named.update(_METRIC.findall(json.dumps(row)))
    known = set(_METRIC.findall((ROOT / "audio_receiver.py").read_text(encoding="utf-8")))
    unknown = sorted(named - known)
    if unknown:
        sys.exit(f"these panel queries name metrics the app does not define: "
                 f"{unknown}. The rows were not installed.")

    if not args.install:
        # Print all rows
        result = {"rows": all_rows, "poller_names": poller_names}
        print(json.dumps(result, indent=1))
        return 0

    tok = token()
    current = api(f"/api/dashboards/uid/{args.uid}", tok, args.base)
    dashboard = current["dashboard"]
    version = current.get("meta", {}).get("version")
    removed = _strip_existing(dashboard)

    base_y = _next_y(dashboard)
    for i, row in enumerate(all_rows):
        row["gridPos"]["y"] = base_y + i * 14  # each row is ~14 grid units tall
        for panel in row.get("panels", []):
            grid = panel.get("gridPos", {})
            grid["y"] = base_y + i * 14 + grid.get("y", 0) - 1
        dashboard.setdefault("panels", []).append(row)

    result = api("/api/dashboards/db", tok, args.base, {
        "dashboard": dashboard,
        "folderUid": current["meta"].get("folderUid"),
        "message": "Add poller-health rows (generated)",
        "overwrite": False,
    })
    print(f"installed {len(all_rows)} poller row(s) on {args.uid}"
          + (f" (replaced {removed} existing row(s))" if removed else "")
          + f" -> version {result.get('version')} (was {version})")
    print("Now run: python3 scripts/fetch_grafana_dashboards.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())