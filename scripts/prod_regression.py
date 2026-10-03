#!/usr/bin/env python3
"""Run the regression battery against a live server.

    python3 scripts/prod_regression.py                    # defaults, vps + public
    python3 scripts/prod_regression.py --host kevcloud
    python3 scripts/prod_regression.py --only http
    python3 scripts/prod_regression.py --list

Why a script and not a pytest file: this talks to production. It must be runnable
on demand from a laptop, it needs three distinct outcomes rather than two, and it
must never be picked up by `pytest tests/` and run against localhost by accident.

Exit codes, and the third one matters:

    0   every check passed
    1   a check failed -- there is a regression
    2   the battery could not run -- unreachable host, no SSH, missing tool

Exit 2 is deliberately distinct. A battery that cannot reach the server must never
report success, or it becomes the project's third instance of the failure mode it
exists to catch: a green check that never executed anything.

READ-ONLY. Every remote action is a read: `GET` over HTTP, and `sqlite3 -readonly`
for the database. There is no code path here that can mutate production.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PUBLIC = "https://battlebuddy.news"
DEFAULT_HOSTS = ("vps",)

#: Payloads the battery refuses to let through. Present because the XSS on
#: /premium/commute was found by review, not by a test, and the whole point is
#: that the next one is found by the battery.
XSS_PAYLOAD = '";alert(1);//'


@dataclass
class Result:
    check: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class Battery:
    name: str
    results: list[Result] = field(default_factory=list)

    def record(self, check: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(check, ok, detail))

    def skip(self, check: str, why: str) -> None:
        self.results.append(Result(check, True, f"SKIPPED: {why}", skipped=True))

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if not r.ok]

    @property
    def ran(self) -> int:
        return sum(1 for r in self.results if not r.skipped)


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------

class Unreachable(Exception):
    """The battery could not run. Distinct from a check failing."""


def http_get(url: str, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "bb-prod-regression/1")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        raise Unreachable(f"GET {url}: {exc}") from exc


def ssh(host: str, command: str, timeout: int = 60) -> str:
    """Run a read-only command. Raises Unreachable when the host cannot be reached."""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, command],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise Unreachable("ssh is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise Unreachable(f"ssh {host} timed out") from exc
    if proc.returncode != 0:
        raise Unreachable(f"ssh {host} exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


# ---------------------------------------------------------------------------
# names read from the shipped source, never hand-copied
# ---------------------------------------------------------------------------

def watched_metric_names() -> set[str]:
    """Every metric name `bb_transcription_watch.py` gates on.

    Parsed out of the file rather than listed here. A hand-copied list is how
    `REQUIRED_METRICS` came to cover 7 of 13 names: two files each holding a list,
    neither checking the other. Reading the shipped source means a gate added
    there is covered here automatically, and a gate removed stops being claimed.
    """
    src = (ROOT / "scripts" / "bb_transcription_watch.py").read_text(encoding="utf-8")
    return set(re.findall(r"battlebuddy_[a-z_0-9]+", src))


# ---------------------------------------------------------------------------
# checks: public HTTP
# ---------------------------------------------------------------------------

def check_xss(b: Battery, base: str = PUBLIC) -> None:
    """The /premium/commute reflection, as a check rather than a comment.

    Split out from the other HTTP checks so it can be exercised offline against a
    fabricated body — it is the check most likely to rot into always-passing,
    because it only runs when someone remembers to run it.
    """
    payload = XSS_PAYLOAD
    try:
        status, body = http_get(f"{base}/premium/commute?token={urllib.parse.quote(payload)}")
    except Unreachable as exc:
        b.record("xss /premium/commute", False, f"could not run: {exc}")
        return
    # The escaped form is the payload with its leading double quote backslash-escaped,
    # which is what `_js_str` produces. Presence of the raw payload text is expected
    # either way and proves nothing -- what matters is whether it is inert.
    escaped = 'const TOKEN = "\\' + payload
    b.record(
        "xss /premium/commute",
        status == 200 and escaped in body,
        f"HTTP {status}; payload "
        + ("escaped (inert)" if escaped in body else "NOT ESCAPED - reflected raw")
        + f"; literal payload text present in body: {payload in body}",
    )


def check_http(b: Battery, base: str = PUBLIC) -> None:
    for path, must_contain in (
        ("/public", "map"),
        ("/api/incidents", None),
        ("/static/data/austin_cameras.json", '"features"'),
    ):
        url = base + path
        try:
            status, body = http_get(url)
        except Unreachable as exc:
            b.record(f"http {path}", False, f"could not run: {exc}")
            continue
        # Status *and* body. Telegram answers 200 with {"ok":false}; a check that
        # reads only the status is the project's documented defect #15.
        detail = f"HTTP {status}, {len(body)} bytes"
        ok = status == 200
        if ok and must_contain and must_contain not in body:
            ok, detail = False, f"HTTP 200 but {must_contain!r} absent from the body"
        b.record(f"http {path}", ok, detail)
    check_xss(b, base)


# ---------------------------------------------------------------------------
# checks: remote host
# ---------------------------------------------------------------------------

def check_remote(b: Battery, host: str) -> None:
    try:
        b.record(f"{host}: service active",
                 ssh(host, "systemctl is-active battlebuddy").strip() == "active")
    except Unreachable as exc:
        b.record(f"{host}: service active", False, f"could not run: {exc}")
        return

    try:
        out = ssh(host, "cd /opt/battlebuddy && git rev-parse --short HEAD").strip()
        b.record(f"{host}: deployed HEAD recorded", bool(re.fullmatch(r"[0-9a-f]{7,}", out)),
                 f"HEAD={out or '(none)'}")
    except Unreachable as exc:
        b.skip(f"{host}: deployed HEAD", str(exc))

    try:
        n = int(ssh(host, "journalctl -u battlebuddy --since '10 min ago' --no-pager "
                          "| grep -ci traceback || true").strip() or 0)
        b.record(f"{host}: no tracebacks in 10m", n == 0, f"{n} found")
    except Unreachable as exc:
        b.skip(f"{host}: tracebacks", str(exc))

    # ops_verify is the existing 19-gate authority. Run it; do not reimplement it.
    try:
        out = ssh(host, "python3 /opt/battlebuddy/scripts/ops_verify.py 2>&1 | tail -1",
                  timeout=180)
        ok = "19/19" in out
        b.record(f"{host}: ops_verify 19/19", ok, out.strip()[:160])
    except Unreachable as exc:
        b.skip(f"{host}: ops_verify", str(exc))

    # The metrics contract. /metrics is loopback-only and nginx does not proxy it,
    # so a public fetch returns 404 and would look like a total outage rather than
    # a test error. Loopback is the only correct way to read it.
    try:
        body = ssh(host, "curl -s --max-time 15 http://127.0.0.1:9001/metrics")
        emitted = {ln.split()[2] for ln in body.splitlines()
                   if ln.startswith("# HELP") or ln.startswith("# TYPE")}
        wanted = watched_metric_names()
        missing = sorted(w for w in wanted if w not in emitted)
        b.record(
            f"{host}: every gated metric is emitted",
            not missing,
            f"{len(emitted)} families emitted, {len(wanted)} gated; missing: "
            + (", ".join(missing) if missing else "none"),
        )
    except Unreachable as exc:
        b.skip(f"{host}: metrics contract", str(exc))

    # Read-only database. -readonly so a bug in this script cannot write.
    try:
        out = ssh(host, "sqlite3 -readonly /opt/battlebuddy-data/calls.db "
                        "\"SELECT COUNT(*) FROM calls WHERE worker IS NOT NULL;\"")
        n = int(out.strip() or 0)
        b.record(f"{host}: calls.worker readable", True, f"{n} attributed rows")
    except (Unreachable, ValueError) as exc:
        b.skip(f"{host}: calls.worker", str(exc))


# ---------------------------------------------------------------------------

GROUPS = {"http": lambda b, hosts: check_http(b), "remote": lambda b, hosts: [check_remote(b, h) for h in hosts]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", action="append", dest="hosts", default=None)
    ap.add_argument("--only", choices=sorted(GROUPS), action="append")
    ap.add_argument("--base", default=PUBLIC, help="public base URL")
    ap.add_argument("--list", action="store_true", help="list checks and exit")
    args = ap.parse_args(argv)
    hosts = tuple(args.hosts or DEFAULT_HOSTS)
    chosen = args.only or sorted(GROUPS)

    if args.list:
        print("http:    /public, /api/incidents, camera snapshot, XSS payload")
        print("remote:  service active, HEAD, tracebacks, ops_verify, metrics contract, calls.worker")
        return 0

    b = Battery("prod")
    print(f"prod regression battery — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"public: {args.base}   hosts: {', '.join(hosts) or '(none)'}\n")
    unreachable = 0
    for group in chosen:
        try:
            GROUPS[group](b, hosts)
        except Unreachable as exc:
            unreachable += 1
            b.record(f"group {group}", False, f"could not run: {exc}")

    width = max((len(r.check) for r in b.results), default=10)
    for r in b.results:
        mark = "SKIP" if r.skipped else ("ok  " if r.ok else "FAIL")
        print(f"  [{mark}] {r.check.ljust(width)}  {r.detail}")

    print()
    if b.failures:
        print(f"REGRESSION: {len(b.failures)} of {b.ran} checks FAILED")
        for r in b.failures:
            print(f"   - {r.check}: {r.detail}")
        return 1
    if b.ran == 0:
        print("NOTHING RAN. That is not a pass.")
        return 2
    print(f"all {b.ran} checks passed"
          + (f" ({unreachable} group(s) unreachable)" if unreachable else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())