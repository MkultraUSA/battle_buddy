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

import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:9001"
RESULTS = []

#: Two missed hourly runs before the battery is declared dead rather than green.
REGRESSION_MAX_AGE_S = 2 * 3600
REGRESSION_RESULTS_PATH = os.environ.get("BB_REGRESSION_RESULTS_PATH") or \
    "/opt/battlebuddy-data/regression/latest.json"

#: Sentinel for a missing metric: worse than any real failure count.
POLLER_MAX_FAILURES = 3

#: A poller that has not succeeded in this many seconds is considered stale.
#: The longest poll interval is 6h, so three missed cycles.
POLLER_MAX_AGE_S = 3 * 6 * 3600

#: Recognised poller metrics, and the label that carries the poller's name.
_POLLER_METRIC_RE = re.compile(r'^battlebuddy_poller_[a-z_]+\{poller="([^"]+)"\}')


def poller_names(metrics_keys) -> list[str]:
    """Poller names taken from the metrics the app actually exported.

    Derived, not hardcoded. The previous hardcoded list carried `reddit-intel`,
    whose ``.start()`` is commented out, so it emits nothing -- and because a
    missing metric is correctly treated as a failure, three gates for it would
    have failed forever. A list that cannot drift is worth more than one that is
    easy to read.

    Sorted so gate ordering is stable between runs, which matters because a
    dashboard that reshuffles is harder to read than one that does not.
    """
    names = set()
    for key in metrics_keys:
        match = _POLLER_METRIC_RE.match(key)
        if match:
            names.add(match.group(1))
    return sorted(names)

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


def regression_results():
    """Read the regression battery's results file, or say why it could not."""
    try:
        with open(REGRESSION_RESULTS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, f"no results at {REGRESSION_RESULTS_PATH}"
    except Exception as exc:
        return None, f"unreadable: {str(exc)[:120]}"
    if not isinstance(data, dict) or "checks" not in data:
        return None, "results file has no checks key"
    return data, "ok"


def poller_health(m: dict, names=None) -> dict[str, dict]:
    """Extract poller health from metrics dict.

    Returns a dict mapping poller name to {"active": bool, "failures": int,
    "age_s": float}. Missing metrics are reported as unhealthy -- a poller that
    has stopped reporting must fail the gate, not quietly pass it.

    `names` defaults to the pollers found in `m`. Passing it explicitly is only
    for tests that need to model a poller which has gone entirely silent.
    """
    if names is None:
        names = poller_names(m.keys())
    health = {}
    for name in names:
        active_key = f'battlebuddy_poller_active{{poller="{name}"}}'
        failures_key = f'battlebuddy_poller_consecutive_failures{{poller="{name}"}}'
        age_key = f'battlebuddy_poller_last_success_age_seconds{{poller="{name}"}}'

        active = m.get(active_key)
        failures = m.get(failures_key)
        age_s = m.get(age_key)

        health[name] = {
            "active": bool(active == 1.0) if active is not None else False,
            "failures": int(failures) if failures is not None else POLLER_MAX_FAILURES + 1,
            "age_s": age_s if age_s is not None else float("inf"),
        }
    return health


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


# ---------------------------------------------------------------------------
# Austin traffic-camera snapshot
# ---------------------------------------------------------------------------
# The camera layer is a static file regenerated on a timer by
# bb-camera-snapshot.timer. Nothing else on the box notices if that timer stops:
# the file keeps serving, the map keeps drawing dots, and the popup keeps
# showing a `generated` date that just quietly stops moving. That is the same
# shape as the two failures the project has already been bitten by -- a gauge
# nobody reads, and an /metrics that returned 200 while serving nothing -- so
# the snapshot gets read here, by a gate that fails.
#
# These gates do not trust the file's own mtime: `generated` is the timestamp
# the fetcher stamped into the payload, which is also what the popup shows a
# user, so the gate and the popup agree by construction.

CAMERA_SNAPSHOT_MAX_AGE_S = 30 * 3600
#: Below this the city's live camera set cannot plausibly have collapsed. The
#: test suite uses the same floor. 820 today; this is a "did the fetch silently
#: stop working" tripwire, not a count anyone expects to hit.
CAMERA_MIN_COUNT = 500


def camera_snapshot():
    """Return the parsed snapshot, or (None, reason) if it is not usable."""
    for rel in ("static/data/austin_cameras.json",
                "static/austin_cameras.json"):
        path = os.path.join(_REPO_ROOT, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            return None, f"unreadable: {e}"
        features = data.get("features")
        if not isinstance(features, list):
            return None, "no features array"
        return data, ""
    return None, ("missing -- the snapshot is generated, not committed; "
                  "run scripts/fetch_austin_cameras.py or start "
                  "bb-camera-snapshot.timer")


def camera_frame_ok(url: str) -> str:
    """HEAD a city frame. Returns '' on success, else why it failed.

    HEAD, not GET: the frames are ~250 KB each and this runs on every ops
    verification. The city serves HEAD correctly, so the status and content
    type are still real evidence without pulling the image.
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0]
            if resp.status != 200:
                return f"HTTP {resp.status}"
            if not ctype.startswith("image/"):
                return f"content-type {ctype or 'missing'}"
            return ""
    except Exception as e:
        return f"{type(e).__name__}: {e}"


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

    # 3b. Hourly regression battery -- the check that says whether the *checks*
    # are still working. Two failure modes, and they are different problems:
    #
    #   * the battery ran and something failed  -> a regression
    #   * the battery did not run at all       -> the alarm is broken, which is
    #     worse, because a broken alarm and a healthy system look identical from
    #     the outside. This is the project's own defect #2: the Telegram watcher
    #     queried three metrics the app never emitted and `get(key, 0.0)` turned
    #     each miss into a healthy zero.
    #
    # So freshness is gated separately from outcome. The timer is hourly with a
    # 30 min jitter, and this allows two missed hours before calling it dead.
    reg, why = regression_results()
    if reg is None:
        gate("regression battery has run", False, why)
    else:
        generated = reg.get("generated") or ""
        age_s = None
        try:
            stamp = datetime.datetime.strptime(generated, "%Y-%m-%dT%H:%M:%SZ")
            age_s = time.time() - stamp.replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            generated = f"unparseable {generated!r}"
        fresh = age_s is not None and 0 <= age_s < REGRESSION_MAX_AGE_S
        gate(f"regression battery fresh (<{REGRESSION_MAX_AGE_S // 3600}h)",
             fresh,
             f"age={age_s:.0f}s" if fresh else f"generated={generated} age={age_s}")
        gate("regression battery has run", True,
             f"{reg.get('ran')} checks at {generated}")
        failed_checks = [c for c in (reg.get("checks") or [])
                         if not c.get("ok") and not c.get("skipped")]
        gate("regression battery all green", not failed_checks,
             "ok" if not failed_checks
             else "; ".join(c.get("check", "?") for c in failed_checks)[:160])

    # 3c. Poller health -- the check that says whether the *pollers* are still
    # running and succeeding. Three failure modes, and they are different problems:
    #
    #   * a poller thread stopped           -> active=0, silent loss of coverage
    #   * a poller is failing repeatedly    -> consecutive_failures > threshold
    #   * a poller hasn't succeeded in too  -> last_success_age > threshold
    #     long (including never: age=-1)
    #
    # So each condition is gated separately. A poller that stops is the failure
    # this gate exists to catch -- the same defect class as the regression battery
    # freshness gate: a stopped alarm and a healthy system look identical from
    # the outside.
    # Derived from the live scrape, so a poller that is disabled cannot leave a
    # permanently red gate behind. If NOTHING reports, that is itself the failure
    # and must say so -- an empty set that produced zero gates would read as a
    # clean bill of health.
    _poller_names = poller_names(m.keys())
    if not _poller_names:
        gate("pollers reporting", False,
             "no battlebuddy_poller_* metrics in the scrape at all")
    health = poller_health(m, _poller_names)
    for name in _poller_names:
        h = health[name]
        # Has-run: the poller metrics exist at all (they always do if service is up,
        # but we gate on active=1 as the "has-run" equivalent for a continuous poller)
        gate(f"poller {name} active", h["active"],
             "running" if h["active"] else "STOPPED")
        # Freshness: last success within threshold, and not -1 (never succeeded)
        fresh = h["age_s"] >= 0 and h["age_s"] < POLLER_MAX_AGE_S
        gate(f"poller {name} fresh (<{POLLER_MAX_AGE_S // 3600}h)",
             fresh,
             f"age={h['age_s']:.0f}s" if fresh else f"age={h['age_s']:.0f}s (threshold={POLLER_MAX_AGE_S}s)")
        # All-green: zero consecutive failures
        gate(f"poller {name} zero failures",
             h["failures"] == 0,
             f"failures={h['failures']}")

    # 4. No fresh tracebacks in service logs
    tbs = journal_tracebacks()
    gate("no tracebacks (15m)", not tbs, f"{len(tbs)} found")

    # 5. Austin traffic-camera snapshot -- generated data nobody else reads
    data, why = camera_snapshot()
    if data is None:
        gate("camera snapshot present", False, why)
    else:
        gate("camera snapshot present", True, "ok")

        features = data.get("features") or []
        gate("camera count sane (>%d)" % CAMERA_MIN_COUNT,
             len(features) > CAMERA_MIN_COUNT,
             f"{len(features)} cameras")

        # Freshness, from the stamp the fetcher wrote and the popup shows.
        generated = data.get("generated") or ""
        age_s = None
        try:
            stamp = datetime.datetime.strptime(generated, "%Y-%m-%dT%H:%M:%SZ")
            age_s = time.time() - stamp.replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            generated = f"unparseable {generated!r}"
        gate(f"camera snapshot fresh (<{CAMERA_SNAPSHOT_MAX_AGE_S // 3600}h)",
             age_s is not None and age_s < CAMERA_SNAPSHOT_MAX_AGE_S,
             f"generated={generated} age="
             f"{age_s / 3600:.1f}h" if age_s is not None
             else f"generated={generated}")

        # Kevin's rule: a camera is only on the map if the city publishes a
        # picture of it. Check the snapshot still honours it, and that the host
        # is really serving. Three evenly spread cameras, not all 820 -- a full
        # sweep would be 820 third-party requests on every verification, which
        # is the thing the browser layer is careful never to do either.
        # `(f.get(...) or {})` rather than `.get(k, {})`: a snapshot with an
        # explicit JSON null would otherwise take this gate out with a
        # TypeError instead of reporting the malformed file it found.
        frames = [(f.get("properties") or {}).get("image") for f in features
                  if isinstance(f, dict)]
        missing = [(f.get("properties") or {}).get("id") for f in features
                   if isinstance(f, dict)
                   and not (f.get("properties") or {}).get("image")]
        gate("every plotted camera has a published frame",
             not missing,
             "ok" if not missing else f"{len(missing)} frameless: {missing[:5]}")

        probes = []
        if frames:
            for i in (0, len(frames) // 2, len(frames) - 1):
                url = frames[i]
                if url:
                    probes.append((url, camera_frame_ok(url)))
        bad = [(u, e) for u, e in probes if e]
        gate(f"camera frames reachable (sample of {len(probes)})",
             bool(probes) and not bad,
             "ok" if probes and not bad
             else "; ".join(f"{u.rsplit('/', 1)[-1]}: {e}" for u, e in bad)[:160])

    failed = [r for r in RESULTS if not r["pass"]]
    if "--json" in sys.argv:
        print(json.dumps({"gates": RESULTS,
                          "failed": len(failed)}, indent=1))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} gates pass")
    return min(len(failed), 10)


if __name__ == "__main__":
    sys.exit(main())
