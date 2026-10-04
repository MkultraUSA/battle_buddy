#!/usr/bin/env python3
"""Stop the error panels counting a non-speech clip as an error.

    python3 scripts/build_error_panels.py              # print the queries
    python3 scripts/build_error_panels.py --install    # write them to Grafana

**The bug.** `modules/transcription.py` records one of five outcomes per clip:

    success  |  empty  |  exception  |  lock_timeout  |  timeout

`empty` means the model returned no text. For a clip of silence or noise that is
**the correct answer**, not a failure -- PR #162 deliberately stopped paying to
transcribe silence and stopped queueing LLM work for it. The taxonomy puts
`empty` beside `success`, not beside the three faults.

Two panels collapsed it into a fault anyway:

  * `Recent Whisper / Transcription Error Logs` matched the literal text
    `empty transcript` in Loki.
  * `Whisper Error Log Rate` included `empty` in
    `status=~"lock_timeout|exception|timeout|empty"`.

Both are wrong in the same direction, and both are wrong *quietly* -- a panel full
of non-errors trains you to ignore it, and an empty one looks like an outage. That
is the same failure as a gate reading a metric the app never emits: a green or
blank result that nobody can trust.

**The durable part is `OUTCOME_CLASSES`, not the queries.** Adding a status to the
taxonomy without classifying it here fails
`tests/test_error_panel_classification.py`, so the next person has to say whether
their new outcome is a fault or an expected result. That is the whole reason this
is a script rather than a query typed into the UI.

Install replaces the two panels by title, so it is idempotent and does not touch
anything else on the dashboard.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_UID = "bb-transcription-quality"
DEFAULT_URL = "https://kevinwatkins.grafana.net"
PROM_UID = "grafanacloud-prom"
LOG_DS_UID = "grafanacloud-logs"

#: Every outcome modules/transcription.py can record, and whether it is a fault.
#:
#: Read from the app by the test, so a new status cannot be added silently. This
#: is the classification; the queries below are derived from it rather than the
#: other way round, because a hand-typed regex is how `empty` got here.
OUTCOME_CLASSES = {
    "success": "expected",
    "empty": "expected",       # no text for the clip; correct for non-speech audio
    "exception": "fault",
    "lock_timeout": "fault",
    "timeout": "fault",
}

FAULT_STATUSES = tuple(sorted(k for k, v in OUTCOME_CLASSES.items() if v == "fault"))
FAULT_MATCH = "|".join(FAULT_STATUSES)

#: The app logs these phrases when a clip yields no text. Expected, so they must
#: not appear in an *error* match -- they get their own panel below.
EXPECTED_LOG_PHRASES = ("empty transcript", "[recv] DROP")

ERROR_LOG_QUERY = (
    '{host="kevcloud", job="battlebuddy.service"} '
    '|~ "(?i)(whisper.*(error|timeout|hung|dropping)|transcription thread hung|model lock)"'
)

ERROR_RATE_QUERY = (
    'sum(rate(battlebuddy_transcription_requests_total'
    '{status=~"' + FAULT_MATCH + '"}[$__rate_interval]))\n'
    "  /\n"
    '  clamp_min(sum(rate(battlebuddy_transcription_requests_total[$__rate_interval])), 1e-9)'
)

NON_SPEECH_RATE_QUERY = (
    'sum(rate(battlebuddy_transcription_requests_total{status="empty"}[$__rate_interval]))\n'
    "  /\n"
    '  clamp_min(sum(rate(battlebuddy_transcription_requests_total[$__rate_interval])), 1e-9)'
)

ERROR_LOG_DESCRIPTION = (
    "Whisper and transcription **faults** only: exceptions, timeouts, a hung "
    "thread, a model lock. Deliberately excludes empty transcripts -- a clip that "
    "yields no text is a recorded outcome (status=\"empty\"), and for non-speech "
    "audio it is the correct one. Those are counted on their own panel, "
    "\"Non-speech clips\", rather than inflating this one.\n\n"
    "Maintained by scripts/build_error_panels.py."
)

ERROR_RATE_DESCRIPTION = (
    "Share of transcription requests that ended in a **fault**: "
    f"{FAULT_MATCH}. `empty` is excluded on purpose -- it is a recorded outcome "
    "rather than a failure, and it has its own panel.\n\n"
    "Maintained by scripts/build_error_panels.py."
)

ERROR_LOG_TITLE = "Recent Whisper / Transcription Error Logs"
ERROR_RATE_TITLE = "Whisper Error Log Rate"
NON_SPEECH_TITLE = "Non-speech clips (no transcript) — share of audio"


def app_outcome_statuses() -> set[str]:
    """The outcome taxonomy, read from the dict that counts per status.

    `_metrics_totals` also contains `started`, which is a lifecycle counter and
    not an outcome -- an earlier version of this function grepped for
    status-shaped strings and picked that up, which made the guard report a
    phantom unclassified outcome. So this reads the keys of `status_counts`,
    which is the dict that only ever holds outcomes.

    Parsed with `ast` rather than a regex, for the same reason.
    """
    tree = ast.parse((ROOT / "modules" / "transcription.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "status_counts" not in targets or not isinstance(node.value, ast.Dict):
            continue
        return {k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


def _ds(uid: str) -> dict:
    return {"type": "prometheus" if uid == PROM_UID else "loki", "uid": uid}


def repaired_error_log_panel(existing: dict) -> dict:
    panel = json.loads(json.dumps(existing))
    panel["datasource"] = _ds(LOG_DS_UID)
    for target in panel.get("targets") or []:
        target["expr"] = ERROR_LOG_QUERY
    panel["description"] = (
        "Whisper and transcription **faults** only: exceptions, timeouts, a hung "
        "thread, a model lock. Deliberately excludes empty transcripts -- a clip "
        "that yields no text is a recorded outcome (`status=\"empty\"`), and for "
        "non-speech audio it is the correct one. Those are counted on their own "
        "panel, \"Non-speech clips\", rather than inflating this one.\n\n"
        "Maintained by scripts/build_error_panels.py."
    )
    panel["title"] = ERROR_LOG_TITLE
    return panel


def non_speech_panel(grid: dict) -> dict:
    return {
        "datasource": _ds(PROM_UID),
        "description": (
            "Share of transcribed clips for which the model returned **no text**. "
            "This is not an error rate. It is the non-speech fraction of the "
            "audio, and it is the number to watch when judging whether the "
            "transcription quality panels are being dragged down by silence "
            "rather than by mangling.\n\n"
            "Maintained by scripts/build_error_panels.py."
        ),
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "decimals": 2,
                "max": 1,
                "min": 0,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "blue", "value": None},
                        {"color": "yellow", "value": 0.15},
                    ],
                },
                "unit": "percentunit",
            },
            "overrides": [],
        },
        "gridPos": grid,
        "options": {
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
        },
        "pluginVersion": "",
        "targets": [{
            "datasource": _ds(PROM_UID),
            "expr": NON_SPEECH_RATE_QUERY,
            "legendFormat": "non-speech share",
            "refId": "A",
        }],
        "title": NON_SPEECH_TITLE,
        "type": "stat",
    }


def _apply(dashboard: dict) -> int:
    """Repair the error panels, and converge the non-speech panel on exactly one.

    Two things this gets right that the first version did not:

    * **The non-speech panel is a sibling of the error log, not a child of it.**
      It was originally nested inside the logs panel, which Grafana accepted
      without complaint -- the version bumped every time -- so the count grew by
      one per install and nothing converged. Panels are siblings positioned by
      `gridPos`; a logs panel does not own children.
    * **Every existing copy is removed before one is added.** Matching on the
      title at the level being walked was not enough, because the walk stops at
      the error-log panel and never visited what was nested inside it.
    """
    changed = 0
    placed = False

    def walk(panels):
        """Repair in place; return the list with non-speech copies removed."""
        nonlocal changed, placed
        out = []
        for panel in (panels or []):
            title = panel.get("title") or ""
            if title == NON_SPEECH_TITLE:
                continue                      # re-added once, below
            if title == ERROR_LOG_TITLE:
                for target in panel.get("targets") or []:
                    if target.get("expr") != ERROR_LOG_QUERY:
                        target["expr"] = ERROR_LOG_QUERY
                        changed += 1
                    target["datasource"] = _ds(LOG_DS_UID)
                panel["datasource"] = _ds(LOG_DS_UID)
                panel["description"] = ERROR_LOG_DESCRIPTION
                # A logs panel owns no children. Earlier installs nested the
                # non-speech panel here and Grafana accepted it silently, so
                # those copies are still sitting inside; drop them rather than
                # skipping past them, or the tree never converges.
                if panel.get("panels"):
                    removed_nested = len(panel["panels"] or [])
                    panel["panels"] = []
                    changed += 0  # not a query change; reported below
                    print(f"  removed {removed_nested} panel(s) wrongly nested "
                          f"inside {ERROR_LOG_TITLE!r}")
                grid = panel.get("gridPos") or {"x": 0, "y": 0, "w": 24, "h": 8}
                out.append(panel)
                if not placed:
                    placed = True
                    out.append(non_speech_panel({
                        "x": grid["x"], "y": grid["y"] + grid["h"],
                        "w": min(6, grid["w"]), "h": 4,
                    }))
                continue
            if title == ERROR_RATE_TITLE:
                for target in panel.get("targets") or []:
                    if target.get("expr") != ERROR_RATE_QUERY:
                        target["expr"] = ERROR_RATE_QUERY
                        changed += 1
                panel["description"] = ERROR_RATE_DESCRIPTION
                out.append(panel)
                continue
            panel["panels"] = walk(panel.get("panels"))
            out.append(panel)
        return out

    dashboard["panels"] = walk(dashboard.get("panels"))
    if not placed:
        raise SystemExit(f"could not find a panel titled {ERROR_LOG_TITLE!r}")
    return changed


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
        sys.exit(f"HTTP {exc.code} for {path}: "
                 f"{exc.read().decode('utf-8', 'replace')[:300]}")
    except Exception as exc:
        sys.exit(f"could not reach {url}: {exc}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--base", default=DEFAULT_URL)
    ap.add_argument("--uid", default=DASHBOARD_UID)
    args = ap.parse_args(argv)

    unknown = sorted(app_outcome_statuses() - set(OUTCOME_CLASSES))
    if unknown:
        sys.exit(f"modules/transcription.py can record outcomes this script has "
                 f"not classified: {unknown}. Add each to OUTCOME_CLASSES as "
                 "'fault' or 'expected' before installing -- an unclassified "
                 "outcome would silently keep the old behaviour.")

    if not args.install:
        print(json.dumps({
            "classification": OUTCOME_CLASSES,
            "error_log_query": ERROR_LOG_QUERY,
            "error_rate_query": ERROR_RATE_QUERY,
            "non_speech_query": NON_SPEECH_RATE_QUERY,
        }, indent=1))
        return 0

    tok = token()
    current = api(f"/api/dashboards/uid/{args.uid}", tok, args.base)
    dashboard = current["dashboard"]
    changed = _apply(dashboard)
    result = api("/api/dashboards/db", tok, args.base, {
        "dashboard": dashboard,
        "folderUid": current["meta"].get("folderUid"),
        "message": "Stop counting non-speech clips as transcription errors",
        "overwrite": False,
    })
    print(f"installed on {args.uid}: {changed} query/queries changed"
          f" -> version {result.get('version')} (was {current['meta'].get('version')})")
    print("Now run: python3 scripts/fetch_grafana_dashboards.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())