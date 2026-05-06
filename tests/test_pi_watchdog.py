"""
Unit tests for modules/pi_watchdog.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _stub_leaf(name: str, **attrs):
    mod = type(sys)(name)
    mod.__name__ = name
    mod.__package__ = name.rsplit(".", 1)[0] if "." in name else name
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


_stub_leaf(
    "modules.config",
    ALERT_EMAIL="alerts@example.test",
    MAILGUN_API_KEY="mailgun",
    MAILGUN_DOMAIN="mg.example.test",
    MAILGUN_FROM="battle@example.test",
    PI1_OP25_URL="http://pi.test:8080/",
    TALK_BASE="http://talk.test/ocs/v2.php/apps/spreed/api/v1",
    TALK_PASS="pass",
    TALK_USER="user",
    _state={"last_call_ts": 10_000.0},
)

import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


pi_watchdog = _load_from_file("modules.pi_watchdog", "modules/pi_watchdog.py")

from modules.pi_watchdog import PiWatchdogService  # noqa: E402


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class _ImmediateThread:
    def __init__(self, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class PiWatchdogServiceTests(unittest.TestCase):
    def service(self, *, now=20_000.0, urlopen=None, subprocess_run=None, alerts=None, queue=None):
        alerts = alerts if alerts is not None else []
        return PiWatchdogService(
            interval=60,
            state={"last_call_ts": now},
            command_queue=queue if queue is not None else [],
            alert_func=alerts.append,
            urlopen=urlopen or (lambda *a, **kw: _Response([{"json_type": "trunk_update"}])),
            subprocess_run=subprocess_run or (lambda *a, **kw: subprocess.CompletedProcess(a, 0, "restarted", "")),
            thread_factory=_ImmediateThread,
            time_func=lambda: now,
            sleep_func=lambda _seconds: None,
        )

    def test_alerts_after_two_pi_reachability_failures(self):
        alerts = []

        def down(*_args, **_kwargs):
            raise OSError("down")

        svc = self.service(urlopen=down, alerts=alerts)
        svc.run_once()
        self.assertEqual(alerts, [])

        svc.run_once()
        self.assertEqual(len(alerts), 1)
        self.assertIn("UNREACHABLE", alerts[0])

    def test_alerts_when_op25_trunk_stops_and_recovers(self):
        alerts = []
        trunk_active = False

        def urlopen(request_or_url, **_kwargs):
            if isinstance(request_or_url, str):
                return _Response([])
            if trunk_active:
                return _Response([{"json_type": "trunk_update"}])
            return _Response([])

        svc = self.service(urlopen=urlopen, alerts=alerts)
        for _ in range(3):
            svc.run_once()

        self.assertEqual(len(alerts), 1)
        self.assertIn("OP25 is NOT returning trunk data", alerts[0])

        trunk_active = True
        svc.run_once()

        self.assertEqual(len(alerts), 2)
        self.assertIn("decoder is active again", alerts[1])

    def test_silence_recovery_alert_resets_counter(self):
        alerts = []
        svc = self.service(now=20_000.0, alerts=alerts)
        svc.state["last_call_ts"] = 20_000.0 - 21 * 60

        svc.run_once()
        self.assertEqual(svc.silence_alert_count, 1)
        self.assertIn("No audio from OP25", alerts[-1])

        svc.state["last_call_ts"] = 20_000.0
        svc.run_once()

        self.assertEqual(svc.silence_alert_count, 0)
        self.assertFalse(svc.calls_were_silent)
        self.assertIn("Audio feed is active again", alerts[-1])

    def test_autorestart_fallback_queues_pi_command(self):
        alerts = []
        queue = []

        def ssh_failure(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 1, "", "no route")

        svc = self.service(now=20_000.0, subprocess_run=ssh_failure, alerts=alerts, queue=queue)
        svc.state["last_call_ts"] = 20_000.0 - 31 * 60

        svc.run_once()

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["cmd"], "restart_op25")
        self.assertIn("Queued OP25 restart", alerts[-1])


if __name__ == "__main__":
    unittest.main()
