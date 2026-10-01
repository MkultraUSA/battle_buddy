#!/usr/bin/env python3
"""Watch Battle Buddy transcription health and alert on Telegram.

Runs from cron on a host that can reach the Battle Buddy VPS (originally
Hostinger), scrapes /metrics over SSH, and messages a Telegram channel when
something crosses a threshold.

Provenance: this lived at /opt/hermes-data/workspace/bb-transcription-watch.py
and was moved into the repository because an untracked alerting script cannot be
reviewed, diffed or tested -- and this one had been silently broken.

Four bugs, all of the same shape: the script read metric names the application
never emitted.

    battlebuddy_raw_audio_queue_pending            -> never existed
    battlebuddy_raw_audio_queue_oldest_age_seconds -> never existed
    battlebuddy_raw_audio_queue_failed             -> never existed

The application exports battlebuddy_backlog_files_pending,
battlebuddy_backlog_files_failed and (since the age metric was added)
battlebuddy_backlog_oldest_age_seconds. Because `metric()` returned 0.0 for a
missing key, every queue gate read 0 forever: `pending >= 25`, `oldest >= 300s`
and `failed >= 1` could never fire. The watcher's own output --
"raw_queue=0 oldest=0s failed=0" -- was fabricated health, not a measurement,
and it looked exactly like a healthy queue.

So `read_metric` now records misses instead of swallowing them, and a scrape that
cannot find a metric the script depends on is itself a fault worth reporting.
That is the general fix: the next renamed metric will announce itself rather than
quietly disabling an alarm.

Gates on the durable backlog:

  * queue unreadable          -- a scan error means we are blind, not healthy
  * oldest clip waiting       -- age, not depth, is the stall signal, because
                                 claim takes a lease and leaves the item in
                                 pending, so a worker that claims and dies
                                 leaves the depth unchanged while the wait grows
  * queue growing             -- depth thresholds
  * clips failing permanently -- moved to the failed directory after max_attempts

Usage: bb_transcription_watch.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENV_FILE = Path("/opt/hermes-data/.env")
DEFAULT_STATE_FILE = Path("/opt/hermes-data/bb-transcription-watch-state.json")
DASHBOARD_URL = (
    "https://kevinwatkins.grafana.net/d/bb-transcription-quality/"
    "battle-buddy-transcription-quality"
)
BB_HOST = "root@kevcloud.ddns.net"
SSH_OPTS = [
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
]
REPEAT_SECS = 3600

#: Minimum transcriptions in the 15m window before a ratio means anything.
#: Traffic is currently ~1 call per 85s, so a 15m window holds roughly 11 calls
#: at best and frequently none at all.
MIN_RATIO_SAMPLES = 5


# --------------------------------------------------------------------------
# Metric names, in one place.
#
# If the application renames one of these the watcher says so out loud instead of
# silently reporting zero, which is how this script spent its life reading 0 for
# three metrics that did not exist.
# --------------------------------------------------------------------------
METRIC_COVERAGE = "battlebuddy_transcript_quality_coverage_ratio"
METRIC_RELIABILITY = "battlebuddy_transcript_quality_reliability_score"
METRIC_IN_PROGRESS = "battlebuddy_transcription_in_progress"
METRIC_COMPLETED = "battlebuddy_transcription_completed"
METRIC_SUCCESS_RATIO = "battlebuddy_transcription_success_ratio"
METRIC_LATENCY_P95 = "battlebuddy_transcription_latency_seconds_p95"
METRIC_QUALITY_CALLS = "battlebuddy_transcript_quality_calls"
METRIC_RSS = "battlebuddy_process_rss_bytes"

METRIC_QUEUE_PENDING = "battlebuddy_backlog_files_pending"
METRIC_QUEUE_FAILED = "battlebuddy_backlog_files_failed"
METRIC_QUEUE_SCAN_ERROR = "battlebuddy_backlog_files_scan_error"
METRIC_QUEUE_OLDEST_AGE = "battlebuddy_backlog_oldest_age_seconds"
METRIC_INGEST_OUTCOMES = "battlebuddy_ingest_outcomes_total"

#: Metrics whose absence means the watcher cannot do its job, with the labels
#: they are actually published under.
#:
#: Labels matter: coverage_ratio and success_ratio only exist as
#: {...window="15m"}, so checking for the bare name reports them missing even
#: though they are present. That mistake was caught by this very check, which is
#: a reasonable argument for having the check.
REQUIRED_METRICS: tuple[tuple[str, dict[str, str]], ...] = (
    (METRIC_COVERAGE, {"window": "15m"}),
    (METRIC_IN_PROGRESS, {}),
    (METRIC_SUCCESS_RATIO, {"window": "15m"}),
    (METRIC_QUEUE_PENDING, {}),
    (METRIC_QUEUE_FAILED, {}),
    (METRIC_QUEUE_SCAN_ERROR, {}),
    (METRIC_QUEUE_OLDEST_AGE, {}),
)


def parse_metrics(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Parse a Prometheus exposition body into {(name, labels): value}."""
    metrics: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        name_part, value_part = parts
        labels: tuple[tuple[str, str], ...] = ()
        if "{" in name_part and name_part.endswith("}"):
            metric_name, label_blob = name_part[:-1].split("{", 1)
            parsed = []
            for piece in label_blob.split(","):
                if "=" not in piece:
                    continue
                key, value = piece.split("=", 1)
                parsed.append((key, value.strip('"')))
            labels = tuple(sorted(parsed))
        else:
            metric_name = name_part
        try:
            value = float(value_part)
        except ValueError:
            continue
        metrics[(metric_name, labels)] = value
    return metrics


@dataclass
class MetricReader:
    """Reads metrics and remembers what it could not find.

    `metrics.get(key, 0.0)` is how a missing metric becomes a healthy reading.
    A reader that records its misses turns that class of bug into a visible one.
    """

    metrics: dict[tuple[str, tuple[tuple[str, str], ...]], float]
    missing: set[str] = field(default_factory=set)

    def read(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        if key not in self.metrics:
            self.missing.add(name)
            return 0.0
        return float(self.metrics[key])

    def has(self, name: str, **labels: str) -> bool:
        return (name, tuple(sorted(labels.items()))) in self.metrics


@dataclass
class Status:
    coverage_15m: float = 0.0
    reliability_15m: float = 0.0
    in_progress: float = 0.0
    lock_timeout_15m: float = 0.0
    timeout_15m: float = 0.0
    exception_15m: float = 0.0
    empty_15m: float = 0.0
    success_ratio_15m: float = 0.0
    samples_15m: float = 0.0
    latency_p95_15m: float = 0.0
    rss_gib: float = 0.0
    queue_pending: float = 0.0
    queue_failed: float = 0.0
    queue_scan_error: float = 0.0
    queue_oldest_age_seconds: float = 0.0
    ingest_backlogged: float = 0.0
    ingest_shed: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def build_status(reader: MetricReader) -> Status:
    r = reader.read
    st = Status(
        coverage_15m=r(METRIC_COVERAGE, window="15m"),
        reliability_15m=r(METRIC_RELIABILITY, window="15m"),
        in_progress=r(METRIC_IN_PROGRESS),
        lock_timeout_15m=r(METRIC_COMPLETED, window="15m", status="lock_timeout"),
        timeout_15m=r(METRIC_COMPLETED, window="15m", status="timeout"),
        exception_15m=r(METRIC_COMPLETED, window="15m", status="exception"),
        empty_15m=r(METRIC_COMPLETED, window="15m", status="empty"),
        success_ratio_15m=r(METRIC_SUCCESS_RATIO, window="15m"),
        latency_p95_15m=r(METRIC_LATENCY_P95, window="15m"),
        rss_gib=r(METRIC_RSS) / 1024 / 1024 / 1024,
        queue_pending=r(METRIC_QUEUE_PENDING),
        queue_failed=r(METRIC_QUEUE_FAILED),
        queue_scan_error=r(METRIC_QUEUE_SCAN_ERROR),
        queue_oldest_age_seconds=r(METRIC_QUEUE_OLDEST_AGE),
    )
    # Sample base for the ratio gates. A ratio with no denominator reads 0.0,
    # which is indistinguishable from total failure: with traffic at roughly one
    # call every 85 seconds, a 15-minute window is often empty, and a 0/0 window
    # would page "success ratio 0.00" on a perfectly healthy system. Found by a
    # dry run against production, where the live value was 1.0 moments later.
    st.samples_15m = (
        st.lock_timeout_15m + st.timeout_15m + st.exception_15m
        + st.empty_15m
        + r(METRIC_COMPLETED, window="15m", status="success")
    )

    # Ingest losses, summed across nodes. Uses the counter added with the
    # backlog work: shed audio is otherwise invisible, because it never reaches
    # the database.
    for node in ("pi5", "broadcastify", "unknown"):
        st.ingest_backlogged += r(METRIC_INGEST_OUTCOMES, reason="backlogged", node=node)
        st.ingest_shed += r(METRIC_INGEST_OUTCOMES, reason="throttled", node=node)
        st.ingest_shed += r(METRIC_INGEST_OUTCOMES, reason="queue_full", node=node)
    return st


#: (predicate, level, message). Evaluated in order; first hit wins per gate.
def evaluate(st: Status, reader: MetricReader) -> tuple[str, list[str]]:
    reasons: list[str] = []
    level = "ok"

    def raise_to(new_level: str, reason: str) -> None:
        nonlocal level
        if new_level == "critical" or level == "ok":
            level = new_level if (level == "ok" or new_level == "critical") else level
        reasons.append(reason)

    # --- can we even see the system? ------------------------------------
    absent = sorted(
        name for name, labels in REQUIRED_METRICS if not reader.has(name, **labels)
    )
    if absent:
        # This is the failure that hid for so long: a renamed metric reads as a
        # healthy zero. Report it instead of reporting the zeros.
        raise_to("critical", f"metrics absent: {', '.join(absent)}")

    if st.queue_scan_error >= 1:
        raise_to(
            "critical",
            "backlog queue unreadable (scan_error=1) -- depth readings are not "
            "trustworthy while this is set",
        )

    # --- transcription health (original gates, unchanged) ---------------
    if st.in_progress >= 12 and level == "ok":
        raise_to("warning", f"transcriptions in flight {st.in_progress:.0f} >= 12")
    if st.lock_timeout_15m >= 10 and level == "ok":
        raise_to("warning", f"model lock timeouts (15m) {st.lock_timeout_15m:.0f} >= 10")
    if st.latency_p95_15m >= 90 and level == "ok":
        raise_to("warning", f"p95 latency {st.latency_p95_15m:.0f}s >= 90s")
    # Ratio gates need a denominator. Below MIN_RATIO_SAMPLES the ratio is
    # meaningless, so say so rather than alerting on noise.
    if st.samples_15m < MIN_RATIO_SAMPLES:
        if level == "ok":
            reasons.append(
                f"ratio gates skipped: only {st.samples_15m:.0f} transcriptions in "
                f"15m (need {MIN_RATIO_SAMPLES})"
            )
    else:
        if st.success_ratio_15m <= 0.55 and level == "ok":
            raise_to("critical", f"success ratio (15m) {st.success_ratio_15m:.2f} <= 0.55")
        if st.coverage_15m <= 0.40 and level == "ok":
            raise_to("critical", f"coverage (15m) {st.coverage_15m:.2f} <= 0.40")
    if st.rss_gib >= 10:
        raise_to("warning", f"RSS {st.rss_gib:.1f}GiB >= 10GiB")

    # --- durable backlog -------------------------------------------------
    # Age first. Depth alone cannot detect a stalled worker: claim takes a lease
    # and leaves the item in pending, so a worker that claims a clip and then
    # dies holds the depth steady while the wait grows without bound.
    if st.queue_pending > 0:
        if st.queue_oldest_age_seconds >= 900:
            raise_to(
                "critical",
                f"oldest backlog clip {st.queue_oldest_age_seconds:.0f}s >= 900s "
                "-- the remote worker is not draining the queue",
            )
        elif st.queue_oldest_age_seconds >= 300:
            raise_to(
                "warning",
                f"oldest backlog clip {st.queue_oldest_age_seconds:.0f}s >= 300s",
            )
    if st.queue_pending >= 100:
        raise_to("critical", f"backlog pending {st.queue_pending:.0f} >= 100")
    elif st.queue_pending >= 25:
        raise_to("warning", f"backlog pending {st.queue_pending:.0f} >= 25")
    if st.queue_failed >= 5:
        raise_to("critical", f"backlog failed {st.queue_failed:.0f} >= 5")
    elif st.queue_failed >= 1:
        raise_to("warning", f"backlog failed {st.queue_failed:.0f} >= 1")

    # Losses are only interesting once they exist; a healthy system has none and
    # that should not page anyone.
    if st.ingest_shed >= 50:
        raise_to("warning", f"{st.ingest_shed:.0f} audio clips discarded at ingest")

    return level, reasons


def summarize(level: str, reasons: list[str], st: Status, log_excerpt: str) -> str:
    lines = [
        f"Battle Buddy transcription watch: {level.upper()}",
        f"coverage15={st.coverage_15m:.2f} reliability15={st.reliability_15m:.2f}",
        f"in_progress={st.in_progress:.0f} lock_timeout15={st.lock_timeout_15m:.0f}",
        f"success_ratio15={st.success_ratio_15m:.2f} n15={st.samples_15m:.0f} "
        f"p95={st.latency_p95_15m:.0f}s rss={st.rss_gib:.1f}GiB",
        f"backlog pending={st.queue_pending:.0f} oldest={st.queue_oldest_age_seconds:.0f}s "
        f"failed={st.queue_failed:.0f}",
        f"ingest queued={st.ingest_backlogged:.0f} shed={st.ingest_shed:.0f}",
    ]
    if reasons:
        lines.append("reasons: " + "; ".join(reasons))
    lines.append("dashboard: " + DASHBOARD_URL)
    if log_excerpt:
        lines.append("recent logs:")
        lines.append(log_excerpt)
    return "\n".join(lines)


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return env
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip('"').strip("'")
    return env


def run_ssh(command: str) -> str:
    proc = subprocess.run(
        ["ssh", *SSH_OPTS, BB_HOST, command],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip()
                           or f"ssh failed: {proc.returncode}")
    return proc.stdout


def fetch_metrics_text() -> str:
    return run_ssh("curl -fsS http://127.0.0.1:9001/metrics")


def fetch_recent_log_excerpt() -> str:
    try:
        out = run_ssh(
            "journalctl -u battlebuddy.service --since '20 minutes ago' --no-pager "
            "| grep -Ei 'whisper|timeout|lock|error|hung|dropping|backlog|raw-queue|retain' "
            "| tail -8"
        ).strip()
    except Exception:
        return ""
    return out


def send_telegram(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"telegram send failed: {payload}")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state), encoding="utf-8")


def decide_notify(
    level: str,
    last_level: str,
    last_sent: int,
    now: int,
    repeat_secs: int = REPEAT_SECS,
) -> tuple[bool, bool]:
    """Should this run message Telegram, and is it a recovery?

    Returns (should_send, is_recovery).

    Extracted from main() because this is the behaviour the operator actually
    relies on -- "it tells me when it gets bad and when it recovers" -- and while
    it lived inline in main() it had no test at all. Two things it must get
    right:

      * a fresh fault notifies immediately, without waiting out the repeat
        interval, including when it changes severity (warning -> critical);
      * returning to healthy notifies once, labelled RECOVERED, and then goes
        quiet again rather than repeating "recovered" every five minutes.
    """
    if level in {"warning", "critical"}:
        if level != last_level or (now - last_sent) >= repeat_secs:
            return True, False
        return False, False
    if last_level in {"warning", "critical"}:
        return True, True
    return False, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print, but do not send Telegram")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    args = parser.parse_args(argv)

    env = load_env(args.env_file)
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_HOME_CHANNEL") or env.get("TELEGRAM_CHAT_ID") or ""

    state = load_state(args.state_file)
    now = int(time.time())

    try:
        reader = MetricReader(parse_metrics(fetch_metrics_text()))
        st = build_status(reader)
        level, reasons = evaluate(st, reader)
        log_excerpt = fetch_recent_log_excerpt() if level != "ok" else ""
        message = summarize(level, reasons, st, log_excerpt)
    except Exception as exc:
        level = "critical"
        reasons = [f"metrics fetch failed: {exc}"]
        st = Status()
        message = summarize(level, reasons, st, "")

    last_level = state.get("level", "unknown")
    last_sent = int(state.get("last_sent", 0))

    should_send, is_recovery = decide_notify(level, last_level, last_sent, now)
    if is_recovery:
        message = "Battle Buddy transcription watch: RECOVERED\n" + message

    if should_send and not args.dry_run:
        if not token or not chat_id:
            print("missing Telegram config; not sending", file=sys.stderr)
        else:
            try:
                send_telegram(token, chat_id, message[:3900])
                state["last_sent"] = now
            except Exception as exc:
                print(f"telegram send failed: {exc}", file=sys.stderr)

    state["level"] = level
    state["updated_at"] = now
    if not args.dry_run:
        save_state(args.state_file, state)

    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())