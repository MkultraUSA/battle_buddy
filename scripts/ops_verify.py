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
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:9001"
RESULTS = []

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------
# The SLO gates must inspect the SAME database the running service uses, or they
# are verifying a file nothing is reading. This script is invoked by CI over a
# plain SSH shell, which does NOT inherit the service's environment, so reading
# os.environ alone silently falls back to a path that no longer holds data.
#
# Load the same EnvironmentFile set, in the same order, that the unit file
# declares, so later files win exactly as systemd resolves them. The list is
# discovered from the unit rather than hardcoded, so a future change to the
# unit cannot drift away from this script again.

_UNIT_ENV_FILES = (
    "systemctl show battlebuddy -p EnvironmentFiles --value",
)


def _service_env_files():
    try:
        out = subprocess.run(
            _UNIT_ENV_FILES, shell=True, capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return []
    files = []
    for entry in out.split():
        # Entries look like "/path/to/file (ignore_errors=no)".
        path = entry.split("(")[0].strip()
        if path:
            files.append(path)
    return files


def load_service_env():
    """Populate os.environ from the service's EnvironmentFile set, in order."""
    for path in _service_env_files():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key:
                        os.environ[key] = value
        except OSError:
            continue


load_service_env()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# modules.config computes DB_PATH at import time. If something already imported
# it earlier in this process, a cached copy would carry a stale value from
# whatever environment existed then, and the gate would silently verify the
# wrong database. Drop any cached copy so the freshly loaded service
# environment is what actually decides DB_PATH.
sys.modules.pop("modules.config", None)
try:
    from modules.config import DB_PATH as DB
except Exception as _exc:  # pragma: no cover - surfaced by the db gate
    DB = ""
    _DB_IMPORT_ERROR = str(_exc)


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
        seed_error = m.get("battlebuddy_homicides_seed_error", 0)
        gate("homicide seed readable", not seed_error, f"error={seed_error:.0f}")
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
