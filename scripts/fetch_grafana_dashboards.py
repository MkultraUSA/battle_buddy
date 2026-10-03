#!/usr/bin/env python3
"""Refresh the committed Grafana dashboard fixtures.

    python3 scripts/fetch_grafana_dashboards.py
    python3 scripts/fetch_grafana_dashboards.py --list

Pulls every Battle Buddy dashboard from Grafana Cloud and writes it to
`fixtures/grafana/` as JSON. `tests/test_grafana_contract.py` then checks the
app's exported metrics against those fixtures **offline** — no token, no network,
no rate limit, nothing to flake.

The split matters. Grafana Cloud is a third party with a token that expires and
rate limits that bite; a CI test that calls it on every push is a flake generator
that teaches people to retry. So the API is used once, deliberately, by a human
or a scheduled job, and what CI reads is a file in the repository.

Run this after changing a dashboard in the UI. A test failure saying a metric the
dashboard reads is not emitted is the signal to run it.

Token: BB_GRAFANA_TOKEN, or ~/.local/share/battlebuddy-homicide-watch/.grafana_token.
Never printed, never written into the fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures" / "grafana"
DEFAULT_URL = "https://kevinwatkins.grafana.net"

#: Only Battle Buddy's own dashboards. The org also holds 35 Grafana Agent,
#: node_exporter and Windows boards that have nothing to do with this app, and
#: including them would dilute the contract check into a tautology.
OWNED = (
    "bb-transcription-quality",
    "bb-intel-public",
    "bb-ops-private",
    "bb-memleak",
    "bb-github-activity",
)

#: Anything matching these in a dashboard is a credential and must never land in
#: a committed file. Checked before writing, not trusted afterwards.
SECRET_MARKERS = ("eyJ", "glsa_", "Bearer ", "basicAuth", "secureJsonData")


def token() -> str:
    env = os.environ.get("BB_GRAFANA_TOKEN")
    if env:
        return env.strip()
    path = Path(os.path.expanduser(
        "~/.local/share/battlebuddy-homicide-watch/.grafana_token"))
    if not path.exists():
        sys.exit(f"no token: set BB_GRAFANA_TOKEN or create {path}")
    return path.read_text(encoding="utf-8").strip()


def api(path: str, tok: str, base: str) -> dict:
    req = urllib.request.Request(base + path, method="GET")
    req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code} for {path}: {exc.read()[:200].decode('utf-8', 'replace')}")
    except Exception as exc:
        sys.exit(f"could not reach {base}{path}: {exc}")


def _redact(node):
    """Strip anything that looks like a credential, recursively."""
    if isinstance(node, dict):
        return {
            k: ("<redacted>" if k in ("secureJsonData", "basicAuthPassword") else _redact(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_redact(v) for v in node]
    if isinstance(node, str):
        for marker in SECRET_MARKERS:
            if marker in node:
                return "<redacted>"
    return node


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_URL)
    ap.add_argument("--list", action="store_true", help="list the org's dashboards and exit")
    ap.add_argument("--check", action="store_true",
                    help="fail if the live dashboards differ from the fixtures")
    args = ap.parse_args(argv)
    tok = token()

    if args.list:
        found = api("/api/search?type=dash-db&limit=200", tok, args.base)
        print(f"{len(found)} dashboards in the org:")
        for row in sorted(found, key=lambda r: r.get("title", "")):
            print(f"  {row['uid']:30} {row.get('title')}")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    drift = []
    for uid in OWNED:
        payload = api(f"/api/dashboards/uid/{uid}", tok, args.base)
        dashboard = _redact(payload.get("dashboard", {}))
        meta = payload.get("meta", {})
        dashboard["__meta__"] = {
            "uid": uid,
            "version": meta.get("version"),
            "updated": meta.get("updated"),
            "fetched_from": args.base,
        }
        text = json.dumps(dashboard, indent=1, sort_keys=True) + "\n"
        for marker in SECRET_MARKERS:
            if marker in text and marker not in ("basicAuth",):
                sys.exit(f"refusing to write {uid}: {marker!r} appears in the payload")
        path = OUT / f"{uid}.json"
        if args.check:
            if not path.exists():
                drift.append(uid)
            elif path.read_text(encoding="utf-8") != text:
                drift.append(uid)
        else:
            path.write_text(text, encoding="utf-8")
        panels = _count_panels(dashboard)
        verb = "drifted" if uid in drift else ("ok" if args.check else "wrote")
        print(f"  {verb:7} {uid:30} v{meta.get('version')}  {panels} panels")

    if args.check:
        if drift:
            print(f"\n{len(drift)} dashboard(s) differ from the fixtures: {', '.join(drift)}")
            print("Run scripts/fetch_grafana_dashboards.py and commit the result.")
            return 1
        print("\nfixtures match the live dashboards")
    return 0


def _count_panels(dashboard: dict) -> int:
    total = 0

    def walk(panels):
        nonlocal total
        for p in panels or []:
            total += 1
            walk(p.get("panels"))

    walk(dashboard.get("panels"))
    return total


if __name__ == "__main__":
    sys.exit(main())