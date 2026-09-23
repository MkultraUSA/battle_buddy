#!/usr/bin/env python3
"""Battle Buddy post-deploy SLO verification.

Closes the loop from observability into CI/CD: after every production
deploy, GitHub Actions SSHes to the VPS and runs this script. It checks
live service health, Prometheus gauges, merge-engine behavior, and data
freshness — the same signals on the Grafana Ops board — and exits non-zero
on any breach so the pipeline fails loudly (Telegram alert included).

Thresholds are SLOs, not tests: they assert production *behavior*,
complementing pytest (which asserts code *correctness*).

Usage: ./venv/bin/python scripts/ops_verify.py [--json]
Exit codes: 0 all gates pass, 1+ count of failed gates (capped at 10).
"""

import json
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:9001"
DB = "/opt/battlebuddy/calls.db"
RESULTS = []


def gate(name, ok, detail=""):
    RESULTS.append({"gate": name, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def http_ok(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            return r.status
    except Exception as e:
        return f"ERR {e}"


def metrics():
    try:
        with urllib.request.urlopen(BASE + "/metrics", timeout=15) as r:
            out = {}
            for line in r.read().decode().splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split()
                if len(parts) == 2:
                    try:
                        out[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
            return out
    except Exception as e:
        return {"_error": str(e)}


def journal_tracebacks(minutes=15):
    try:
        r = subprocess.run(
            ["journalctl", "-u", "battlebuddy.service", "--no-pager",
             "--since", f"{minutes} min ago"],
            capture_output=True, text=True, timeout=30,
        )
        return [ln for ln in r.stdout.splitlines()
                if "Traceback" in ln or "ModuleNotFoundError" in ln]
    except Exception as e:
        return [f"journal unreadable: {e}"]


def main():
    now = time.time()

    # 1. HTTP surface — the pages and APIs users and Grafana hit
    for path in ["/public", "/public/homicides", "/api/incidents",
                 "/api/incidents/active", "/api/homicides"]:
        st = http_ok(path)
        gate(f"http {path}", st == 200, str(st))

    # 2. Prometheus gauges — same signals as the Ops board
    m = metrics()
    if "_error" in m:
        gate("metrics endpoint", False, m["_error"])
    else:
        gate("metrics endpoint", True, f"{len(m)} gauges")
        gate("backlog depth < 50",
             m.get("battlebuddy_backlog_queue_depth", 999) < 50,
             str(m.get("battlebuddy_backlog_queue_depth")))
        gate("active incidents < 20",
             m.get("battlebuddy_active_incidents", 999) < 20,
             str(m.get("battlebuddy_active_incidents")))
        newest = m.get("battlebuddy_homicides_seed_newest_ts", 0)
        gate("homicide data fresh (<14d)",
             (now - newest) < 14 * 86400 if newest else False,
             f"age={(now - newest) / 86400:.1f}d" if newest else "missing")

    # 3. Pipeline alive + merge engine sane (last 60 min of DB truth)
    try:
        con = sqlite3.connect(DB)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM calls WHERE ts >= ?", (now - 3600,))
        calls = cur.fetchone()[0]
        gate("calls flowing (1h > 0)", calls > 0, str(calls))
        cur.execute(
            "SELECT COUNT(*) FROM incidents WHERE ts_start >= ? "
            "AND (is_test IS NULL OR is_test = 0)", (now - 3600,))
        created = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM incidents WHERE ts_updated >= ? "
            "AND ts_start < ? AND (is_test IS NULL OR is_test = 0)",
            (now - 3600, now - 3600))
        merged = cur.fetchone()[0]
        gate("merge ratio sane (merged <= 2x created)",
             merged <= max(1, created * 2), f"created={created} merged={merged}")
        cur.execute(
            "SELECT COUNT(*) FROM incidents WHERE status='active' "
            "AND lat IS NULL AND (is_test IS NULL OR is_test = 0)")
        gate("no unlocated active incidents",
             cur.fetchone()[0] == 0, "ok")
        con.close()
    except Exception as e:
        gate("db checks", False, str(e)[:120])

    # 4. No fresh tracebacks in service logs
    tbs = journal_tracebacks()
    gate("no tracebacks (15m)", not tbs, f"{len(tbs)} found")

    failed = [r for r in RESULTS if not r["pass"]]
    if "--json" in sys.argv:
        print(json.dumps({"gates": RESULTS,
                          "failed": len(failed)}, indent=1))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} gates pass")
    return min(len(failed), 10)


if __name__ == "__main__":
    sys.exit(main())
