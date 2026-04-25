import threading
import time
import json
import urllib.request
import urllib.parse

# Placeholder imports/constants that will be needed or provided by context
# Real implementation will depend on how audio_receiver.py is structured and initialized.

class HoldEngine:
    def __init__(self, pi_op25_url, hold_enabled=True, hold_release_minutes=5):
        self.pi_op25_url = pi_op25_url
        self.hold_enabled = hold_enabled
        self.hold_release_minutes = hold_release_minutes
        self.current_hold_tgid = None
        self.last_hold_activity = time.time()
        self.hold_lock = threading.Lock()

    def consider_hold(self, tgid, itype, escalation_stage=None, escalation_min_tier=None, tgid_tier=None):
        if tgid == 0:
            return
            
        with self.hold_lock:
            if self.current_hold_tgid is None:
                self._send_hold(tgid)
                return
            
            if self.current_hold_tgid == tgid:
                self.last_hold_activity = time.time()
                return
                
            if escalation_stage and escalation_min_tier and tgid_tier:
                min_tier = escalation_min_tier.get(escalation_stage, 1)
                new_tier = tgid_tier.get(tgid, 0)
                cur_tier = tgid_tier.get(self.current_hold_tgid, 0)
                
                if new_tier >= min_tier and new_tier > cur_tier:
                    print(f"[hold] ESCALATION {escalation_stage}: tier {cur_tier} TGID {self.current_hold_tgid}"
                          f" → tier {new_tier} TGID {tgid}", flush=True)
                    self._send_hold(tgid)
                    return
                
            # Keep updated even if no escalation
            self.last_hold_activity = time.time()

    def _send_hold(self, tgid):
        payload = json.dumps([{"command": "hold", "arg1": tgid, "arg2": 0}]).encode()
        try:
            req = urllib.request.Request(
                self.pi_op25_url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
            self.current_hold_tgid = tgid
            self.last_hold_activity = time.time()
            print(f"[hold] HOLD  TGID {tgid}", flush=True)
        except Exception as e:
            print(f"[hold] FAILED to hold TGID {tgid}: {e}", flush=True)

    def _send_skip(self):
        payload = json.dumps([{"command": "skip", "arg1": 0, "arg2": 0}]).encode()
        try:
            req = urllib.request.Request(
                self.pi_op25_url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
            prev = self.current_hold_tgid
            self.current_hold_tgid = None
            print(f"[hold] SKIP  (released TGID {prev})", flush=True)
        except Exception as e:
            print(f"[hold] FAILED to release: {e}", flush=True)

    def hold_watchdog_thread(self):
        while True:
            time.sleep(30)
            with self.hold_lock:
                if (self.current_hold_tgid is not None and
                        time.time() - self.last_hold_activity > self.hold_release_minutes * 60):
                    print(f"[hold] watchdog: releasing TGID {self.current_hold_tgid} (timeout)", flush=True)
                    if self.hold_enabled:
                        self._send_skip()
