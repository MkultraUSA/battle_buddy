"""
modules/pi_watchdog.py
~~~~~~~~~~~~~~~~~~~~~~
Pi / OP25 watchdog service.

Tracks Pi reachability, OP25 trunk decoder health, and radio-call silence.
Queues restart commands for the Pi when direct SSH restart fails.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from modules.config import (
    ALERT_EMAIL,
    MAILGUN_API_KEY,
    MAILGUN_DOMAIN,
    MAILGUN_FROM,
    PI1_OP25_URL,
    TALK_BASE,
    TALK_PASS,
    TALK_USER,
    _state,
)

PI_WATCHDOG_INTERVAL = 60
PI_CALL_SILENCE_MINS = 20
PI_ALERT_REPEAT_MINS = 20
PI_AUTORESTART_MINS = 30
PI_ALERT_USERS = ["kevin"]
PI1_OP25_CMD_URL = os.environ.get("PI1_OP25_CMD_URL", "http://radio-node.example.local:8080/")
PI1_SSH_HOST = os.environ.get("PI1_SSH_HOST", "radio-node.example.local")
PI1_SSH_USER = "pi"
PI1_SSH_KEY = "/root/.ssh/id_ed25519"

_pi_command_queue = []
_default_service: PiWatchdogService | None = None


class PiWatchdogService:
    """Background service for Pi reachability, OP25, and audio-silence checks."""

    def __init__(
        self,
        interval: float = PI_WATCHDOG_INTERVAL,
        call_silence_mins: float = PI_CALL_SILENCE_MINS,
        alert_repeat_mins: float = PI_ALERT_REPEAT_MINS,
        autorestart_mins: float = PI_AUTORESTART_MINS,
        pi_url: str = PI1_OP25_URL,
        op25_cmd_url: str = PI1_OP25_CMD_URL,
        state: dict | None = None,
        command_queue: list | None = None,
        alert_func=None,
        urlopen=None,
        subprocess_run=None,
        thread_factory=None,
        time_func=None,
        sleep_func=None,
    ) -> None:
        self.interval = interval
        self.call_silence_mins = call_silence_mins
        self.alert_repeat_mins = alert_repeat_mins
        self.autorestart_mins = autorestart_mins
        self.pi_url = pi_url
        self.op25_cmd_url = op25_cmd_url
        self.state = state if state is not None else _state
        self.command_queue = command_queue if command_queue is not None else _pi_command_queue
        self.alert = alert_func or _pi_watchdog_alert
        self.urlopen = urlopen or urllib.request.urlopen
        self.subprocess_run = subprocess_run or subprocess.run
        self.thread_factory = thread_factory or threading.Thread
        self.time = time_func or time.time
        self.sleep = sleep_func or time.sleep
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run_forever, daemon=True)

        self.pi_was_down = False
        self.op25_was_dead = False
        self.op25_fail_count = 0
        self.pi_fail_count = 0
        self.calls_were_silent = False
        self.last_silence_alert = 0.0
        self.silence_alert_count = 0
        self.last_autorestart_ts = 0.0

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            self.sleep(self.interval)
            if self.stop_event.is_set():
                return
            self.run_once()

    def run_once(self) -> None:
        pi_up = self._check_pi_reachable()
        self._handle_pi_reachability(pi_up)
        if pi_up:
            self._handle_op25_health()
        self._handle_audio_silence(pi_up)

    def _check_pi_reachable(self) -> bool:
        try:
            self.urlopen(self.pi_url, timeout=10)
            return True
        except Exception:
            return False

    def _handle_pi_reachability(self, pi_up: bool) -> None:
        if not pi_up:
            self.pi_fail_count += 1
            if self.pi_fail_count >= 2 and not self.pi_was_down:
                self.pi_was_down = True
                self.alert(
                    f"BATTLE BUDDY ALERT: Pi 1 (OP25) is UNREACHABLE at {self.pi_url} - radio feed is down."
                )
            return

        self.pi_fail_count = 0
        if self.pi_was_down:
            self.pi_was_down = False
            self.alert("Pi 1 (OP25) is back online - radio feed restored.")

    def _handle_op25_health(self) -> None:
        if not self._poll_op25_trunk():
            self.op25_fail_count += 1
            if self.op25_fail_count >= 3 and not self.op25_was_dead:
                self.op25_was_dead = True
                self.alert(
                    "BATTLE BUDDY ALERT: Pi is up but OP25 is NOT returning trunk data - "
                    "decoder may have crashed or lost the control channel."
                )
            return

        self.op25_fail_count = 0
        if self.op25_was_dead:
            self.op25_was_dead = False
            self.alert("OP25 trunk decoder is active again - feed restored.")

    def _handle_audio_silence(self, pi_up: bool) -> None:
        silence_secs = self.time() - self.state["last_call_ts"]
        if silence_secs > self.call_silence_mins * 60:
            now = self.time()
            since_last_alert = now - self.last_silence_alert
            if not self.calls_were_silent or since_last_alert >= self.alert_repeat_mins * 60:
                self.calls_were_silent = True
                self.silence_alert_count += 1
                self.last_silence_alert = now
                mins = int(silence_secs // 60)
                suffix = f" (reminder #{self.silence_alert_count})" if self.silence_alert_count > 1 else ""
                self.alert(
                    f"BATTLE BUDDY ALERT: No audio from OP25 for {mins} minutes{suffix} - "
                    "check SDR or collector."
                )
            if silence_secs > self.autorestart_mins * 60 and pi_up:
                self.thread_factory(target=self._autorestart_op25, daemon=True).start()
        elif self.calls_were_silent:
            self.calls_were_silent = False
            self.silence_alert_count = 0
            self.last_silence_alert = 0.0
            self.alert("Audio feed is active again - calls resuming.")

    def _poll_op25_trunk(self) -> bool:
        try:
            cmd = json.dumps([{"command": "update", "arg1": 0, "arg2": 0}]).encode()
            req = urllib.request.Request(
                self.op25_cmd_url,
                data=cmd,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = json.loads(self.urlopen(req, timeout=5).read())
            return any(m.get("json_type") == "trunk_update" for m in resp)
        except Exception:
            return False

    def _autorestart_op25(self) -> None:
        now = self.time()
        if now - self.last_autorestart_ts < 300:
            return
        self.last_autorestart_ts = now
        print("[watchdog] AUTO-RESTART: SSHing to Pi to restart op25-multi_rx + call_recorder...", flush=True)
        cmd = (
            "sudo systemctl restart op25-multi_rx && sleep 5 && "
            "systemctl --user restart call_recorder && "
            "echo restarted"
        )
        try:
            result = self.subprocess_run(
                [
                    "ssh",
                    "-i",
                    PI1_SSH_KEY,
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "BatchMode=yes",
                    f"{PI1_SSH_USER}@{PI1_SSH_HOST}",
                    cmd,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0 and "restarted" in result.stdout:
                print("[watchdog] AUTO-RESTART: SSH success - op25-multi_rx + call_recorder restarted", flush=True)
                self.alert("BATTLE BUDDY: Auto-restarted OP25 + call_recorder via SSH - monitoring for recovery.")
            else:
                raise RuntimeError(result.stderr.strip() or f"rc={result.returncode}")
        except Exception as exc:
            print(f"[watchdog] AUTO-RESTART: SSH failed ({exc}), queuing command for Pi poller", flush=True)
            self.command_queue.append({"cmd": "restart_op25", "ts": now})
            self.alert("BATTLE BUDDY: Queued OP25 restart - Pi will execute within 60s.")


def _send_email_alert(subject: str, body: str) -> None:
    try:
        creds = base64.b64encode(f"api:{MAILGUN_API_KEY}".encode()).decode()
        payload = urllib.parse.urlencode(
            {
                "from": MAILGUN_FROM,
                "to": ALERT_EMAIL,
                "subject": subject,
                "text": body,
            }
        ).encode()
        req = urllib.request.Request(
            f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
            data=payload,
            headers={"Authorization": f"Basic {creds}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
        print(f"[email] sent to {ALERT_EMAIL}: {subject}", flush=True)
    except Exception as exc:
        print(f"[email] failed: {exc}", flush=True)


def _pi_watchdog_alert(msg: str) -> None:
    """Send a DM alert to watchdog users with email as a parallel channel."""
    print(f"[watchdog] ALERT: {msg}", flush=True)
    threading.Thread(target=_send_email_alert, args=(f"Battle Buddy: {msg[:60]}", msg), daemon=True).start()
    for username in PI_ALERT_USERS:
        sent = False
        for attempt in range(5):
            from modules.talk import _get_or_create_dm_room  # noqa: PLC0415

            token = _get_or_create_dm_room(username)
            if not token:
                time.sleep(5)
                continue
            creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
            payload = urllib.parse.urlencode({"message": msg}).encode()
            req = urllib.request.Request(
                f"{TALK_BASE}/chat/{token}",
                data=payload,
                headers={
                    "Authorization": f"Basic {creds}",
                    "OCS-APIRequest": "true",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                print(f"[watchdog] DM sent to {username}: {msg}", flush=True)
                sent = True
                break
            except Exception as exc:
                print(f"[watchdog] DM attempt {attempt + 1} failed for {username}: {exc}", flush=True)
                time.sleep(10 * (attempt + 1))
        if not sent:
            print(f"[watchdog] CRITICAL: could not deliver alert to {username} after 5 attempts", flush=True)


def get_default_service() -> PiWatchdogService:
    global _default_service
    if _default_service is None:
        _default_service = PiWatchdogService()
    return _default_service


def pi_watchdog_thread() -> None:
    """Backward-compatible raw-thread entry point."""
    get_default_service().run_forever()
