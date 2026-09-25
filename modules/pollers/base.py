"""
modules/pollers/base.py
~~~~~~~~~~~~~~~~~~~~~~~
Abstract base class for all Battle Buddy background pollers.

Subclasses implement run() with their fetch/process logic.
BasePoller manages the thread lifecycle: start/stop and the poll loop.
"""

import abc
import math
import random
import re
import threading
import time

_MAX_HEALTH_ENTRIES = 32
_MAX_HEALTH_FAILURES = 1_000_000
_HEALTH_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class BasePoller(abc.ABC):
    """Abstract poller that runs run() in a daemon thread every `interval` seconds."""

    _health_registry_lock = threading.RLock()
    _health_registry: dict[str, "BasePoller"] = {}

    def __init__(self, interval: float = 300) -> None:
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self._health_state_lock = threading.Lock()
        self._consecutive_failures = 0
        self._last_success_ts = 0.0
        self._health_name = self._get_health_name()
        self._register_health()

    def _get_health_name(self) -> str | None:
        try:
            name = getattr(type(self), "NAME", type(self).__name__)
            if not isinstance(name, str):
                return None
            name = name.strip()
        except Exception:
            return None
        return name if _HEALTH_NAME_RE.fullmatch(name) else None

    def _register_health(self) -> None:
        if self._health_name is None:
            return
        with BasePoller._health_registry_lock:
            existing = BasePoller._health_registry.get(self._health_name)
            if existing is self:
                return
            try:
                existing_alive = existing is not None and existing.thread.is_alive()
            except Exception:
                existing_alive = False
            if existing_alive:
                return
            if (
                self._health_name not in BasePoller._health_registry
                and len(BasePoller._health_registry) >= _MAX_HEALTH_ENTRIES
            ):
                for old_name, old_poller in tuple(BasePoller._health_registry.items()):
                    try:
                        old_alive = old_poller.thread.is_alive()
                    except Exception:
                        old_alive = False
                    if not old_alive:
                        del BasePoller._health_registry[old_name]
                        break
                else:
                    return
            BasePoller._health_registry[self._health_name] = self

    @property
    def consecutive_failures(self) -> int:
        with self._health_state_lock:
            return self._consecutive_failures

    @property
    def last_success_ts(self) -> float:
        with self._health_state_lock:
            return self._last_success_ts

    def _record_success(self) -> None:
        try:
            now = float(time.time())
        except Exception:
            now = 0.0
        if not math.isfinite(now):
            now = 0.0
        with self._health_state_lock:
            self._consecutive_failures = 0
            if now > 0:
                self._last_success_ts = now

    def _record_failure(self) -> None:
        with self._health_state_lock:
            self._consecutive_failures = min(
                self._consecutive_failures + 1,
                _MAX_HEALTH_FAILURES,
            )

    def start(self) -> None:
        """Start the background polling thread."""
        self.thread.start()

    def stop(self) -> None:
        """Signal the loop to stop and wait for the thread to finish."""
        self.stop_event.set()
        self.thread.join()

    def _loop(self) -> None:
        backoff_delay = float(self.interval)
        if self.stop_event.is_set():
            return
        while not self.stop_event.is_set():
            try:
                self.run()
                self._record_success()
                backoff_delay = float(self.interval)
                delay = backoff_delay
            except Exception as e:
                self._record_failure()
                consecutive_failures = self.consecutive_failures
                # Exponential backoff on repeated failures so a down
                # upstream (Reddit/ADSB rate-limit, etc.) is not hammered
                # at full rate forever. Capped at max(interval, 1h) so
                # small-interval pollers back off, large-interval pollers
                # never retry faster than their normal cadence.
                cap = max(float(self.interval), 3600.0)
                delay = backoff_delay
                backoff_delay = min(backoff_delay * 2.0, cap)
                print(
                    "Poller error: "
                    + str(e)
                    + f" (failure #{consecutive_failures},"
                    + f" retry in {delay:.1f}s)"
                )
            # +/-10% jitter to avoid thundering-herd alignment.
            jitter = delay * 0.1 * (random.random() * 2 - 1)
            delay = max(0.5, delay + jitter)
            # Interruptible sleep so stop() returns promptly.
            if self.stop_event.wait(delay):
                return

    @abc.abstractmethod
    def run(self) -> None:  # pragma: no cover
        """Perform one poll cycle. Called by _loop every `interval` seconds."""


def get_poller_health(now: float | None = None) -> list[dict]:
    """Return a bounded snapshot of registered poller health state."""
    try:
        current_time = time.time() if now is None else float(now)
        if not math.isfinite(current_time):
            return []
    except Exception:
        return []

    with BasePoller._health_registry_lock:
        pollers = list(BasePoller._health_registry.items())

    health = []
    for name, poller in pollers:
        try:
            with poller._health_state_lock:
                consecutive_failures = max(
                    0,
                    min(int(poller._consecutive_failures), _MAX_HEALTH_FAILURES),
                )
                last_success_ts = float(poller._last_success_ts)
            if not math.isfinite(last_success_ts):
                last_success_ts = 0.0
            active = bool(poller.thread.is_alive())
            last_success_age = (
                -1.0
                if last_success_ts <= 0
                else max(0.0, current_time - last_success_ts)
            )
        except Exception:
            continue
        health.append({
            "name": name,
            "consecutive_failures": consecutive_failures,
            "last_success_ts": last_success_ts,
            "last_success_age_seconds": last_success_age,
            "active": active,
        })
    return sorted(health, key=lambda item: item["name"])
