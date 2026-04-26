"""modules/pollers/base.py

Abstract base class for all Battle Buddy pollers.

Design goals:
  - Zero circular imports: only stdlib imports (abc, threading, time, logging).
    No modules.config / modules.database / etc. at import time.
  - Thread-safe lifecycle management via threading.Event stop-flag.
  - Deterministic exponential backoff with configurable cap and jitter.
  - Concrete subclasses implement exactly one method: fetch().

Usage
-----
    from modules.pollers.base import BasePoller

    class MyPoller(BasePoller):
        NAME     = "my_poller"
        INTERVAL = 60          # seconds between successful polls

        def fetch(self):
            # do the work; raise any exception on transient failure
            result = _call_some_api()
            return result

    poller = MyPoller()
    poller.start()    # launches daemon thread
    ...
    poller.stop()     # graceful shutdown
"""

from __future__ import annotations

import abc
import logging
import math
import random
import threading
import time
from typing import Any

__all__ = ["BasePoller"]

_log = logging.getLogger(__name__)


class BasePoller(abc.ABC):
    """Abstract base for all event-driven pollers.

    Subclass protocol
    -----------------
    NAME (str)       : human-readable tag used in log messages.
                       Defaults to the concrete class name.
    INTERVAL (float) : target seconds between successful fetch() calls.
                       Defaults to 60.

    Thread model
    ------------
    start() spawns exactly one daemon thread.  The thread loops:

        1. Call fetch().
        2. On success: sleep INTERVAL, then repeat.
        3. On exception: apply exponential backoff (capped at MAX_BACKOFF),
           log the error at WARNING level, then retry.
        4. If the stop-flag is set, exit cleanly after the current sleep.

    Thread safety
    -------------
    * _stop_event  -- threading.Event; set by stop(), checked each sleep tick.
    * _lock        -- threading.Lock; guards _fail_count and _last_error.
    * start() / stop() are safe to call from any thread.
    * Calling start() on an already-running poller raises RuntimeError.

    Backoff
    -------
    After each consecutive failure the sleep is:

        min(BACKOFF_BASE * 2 ** fail_count, MAX_BACKOFF) + jitter

    where jitter is uniform [0, BACKOFF_JITTER_MAX] seconds.
    The counter resets on the first successful fetch().
    """

    # ------------------------------------------------------------------ #
    # Class-level tunables — override in subclass if needed               #
    # ------------------------------------------------------------------ #
    NAME: str = ""                 # falls back to class.__name__ if empty
    INTERVAL: float = 60.0        # seconds between successful polls

    BACKOFF_BASE: float = 5.0     # initial retry delay (seconds)
    MAX_BACKOFF: float = 300.0    # maximum retry delay (5 minutes)
    BACKOFF_JITTER_MAX: float = 5.0  # max random jitter added to each backoff

    # How long to sleep in each tick while waiting; smaller = faster stop()
    _TICK: float = 0.5

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def __init__(self) -> None:
        self._stop_event: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock: threading.Lock = threading.Lock()
        self._fail_count: int = 0
        self._last_error: BaseException | None = None
        self._poll_count: int = 0       # total successful fetch() calls
        self._name: str = self.NAME or self.__class__.__name__

    def start(self) -> None:
        """Spawn the poller daemon thread.  Safe to call once per instance.

        Raises RuntimeError if already running.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(
                    f"[{self._name}] poller is already running; "
                    "call stop() before starting again"
                )
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"poller-{self._name}",
                daemon=True,
            )
            self._thread.start()
        _log.info("[%s] poller started (interval=%ss)", self._name, self.INTERVAL)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the poller thread to stop and wait up to *timeout* seconds.

        Safe to call even if the poller was never started.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                _log.warning(
                    "[%s] poller thread did not stop within %.1fs — "
                    "it will exit on next tick",
                    self._name,
                    timeout,
                )
        _log.info("[%s] poller stopped", self._name)

    @property
    def is_running(self) -> bool:
        """True if the poller thread is alive."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ #
    # Abstract interface                                                   #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def fetch(self) -> Any:
        """Perform one polling cycle.

        Called by the run loop.  Must be implemented by every concrete
        subclass.

        Return value is ignored by the base class — subclasses may return
        data for their own internal use.

        Raise any exception to signal a transient failure; the base class
        will apply exponential backoff and retry automatically.

        To signal a *permanent* (unrecoverable) failure that should stop
        the poller, raise SystemExit or call self.stop() inside fetch().
        """

    # ------------------------------------------------------------------ #
    # Optional hooks                                                       #
    # ------------------------------------------------------------------ #

    def on_start(self) -> None:
        """Called once inside the thread, before the first fetch().

        Override for per-thread initialisation (e.g. open a DB connection).
        Must not raise; exceptions are caught and logged.
        """

    def on_stop(self) -> None:
        """Called once inside the thread, after the loop exits.

        Override for cleanup (e.g. close a DB connection).
        Must not raise; exceptions are caught and logged.
        """

    def on_error(self, exc: BaseException, fail_count: int) -> None:
        """Called after each failed fetch(), before the backoff sleep.

        The default implementation logs a WARNING.  Override to add
        custom alerting or metrics without changing backoff behaviour.

        Parameters
        ----------
        exc        : the exception raised by fetch()
        fail_count : consecutive failure count (1 on first failure)
        """
        _log.warning(
            "[%s] fetch error #%d: %s: %s",
            self._name,
            fail_count,
            type(exc).__name__,
            exc,
        )

    def on_success(self) -> None:
        """Called after each *successful* fetch(), before the interval sleep.

        Override for custom success-side metrics or logging.
        """

    # ------------------------------------------------------------------ #
    # Internal run loop                                                    #
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        """Main thread body.  Not part of the public API."""
        _log.debug("[%s] thread started", self._name)

        # on_start hook — swallow all exceptions so the loop still starts
        try:
            self.on_start()
        except Exception as exc:  # noqa: BLE001
            _log.error("[%s] on_start() raised %s: %s", self._name, type(exc).__name__, exc)

        while not self._stop_event.is_set():
            try:
                result = self.fetch()
                # Success path ----------------------------------------
                with self._lock:
                    self._fail_count = 0
                    self._last_error = None
                    self._poll_count += 1

                try:
                    self.on_success()
                except Exception as exc:  # noqa: BLE001
                    _log.error(
                        "[%s] on_success() raised %s: %s",
                        self._name, type(exc).__name__, exc,
                    )

                self._interruptible_sleep(self.INTERVAL)

            except SystemExit:
                _log.info("[%s] fetch() requested shutdown via SystemExit", self._name)
                self._stop_event.set()
                break

            except Exception as exc:  # noqa: BLE001
                # Failure path ----------------------------------------
                with self._lock:
                    self._fail_count += 1
                    fail_count = self._fail_count
                    self._last_error = exc

                try:
                    self.on_error(exc, fail_count)
                except Exception as hook_exc:  # noqa: BLE001
                    _log.error(
                        "[%s] on_error() itself raised %s: %s",
                        self._name, type(hook_exc).__name__, hook_exc,
                    )

                backoff = self._backoff_for(fail_count)
                _log.debug("[%s] backing off %.1fs (fail #%d)", self._name, backoff, fail_count)
                self._interruptible_sleep(backoff)

        # on_stop hook
        try:
            self.on_stop()
        except Exception as exc:  # noqa: BLE001
            _log.error("[%s] on_stop() raised %s: %s", self._name, type(exc).__name__, exc)

        _log.debug("[%s] thread exiting", self._name)

    def _backoff_for(self, fail_count: int) -> float:
        """Return the backoff delay for *fail_count* consecutive failures.

        Formula:  min(BACKOFF_BASE * 2^(fail_count-1), MAX_BACKOFF)
                  + uniform(0, BACKOFF_JITTER_MAX)

        fail_count is 1-indexed (first failure = 1).
        """
        exponent = max(0, fail_count - 1)
        # guard against math overflow for very large fail_count
        try:
            raw = self.BACKOFF_BASE * math.pow(2.0, exponent)
        except OverflowError:
            raw = self.MAX_BACKOFF
        capped = min(raw, self.MAX_BACKOFF)
        jitter = random.uniform(0.0, self.BACKOFF_JITTER_MAX)
        return capped + jitter

    def _interruptible_sleep(self, duration: float) -> None:
        """Sleep for *duration* seconds, waking early if stop() is called.

        Uses short _TICK increments so stop() is responsive.
        """
        deadline = time.monotonic() + duration
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self._TICK, remaining))

    # ------------------------------------------------------------------ #
    # Diagnostics                                                          #
    # ------------------------------------------------------------------ #

    def diagnostics(self) -> dict:
        """Return a snapshot of poller health metrics.

        Returns a plain dict with no external dependencies — safe to
        call from any context including the web UI.
        """
        with self._lock:
            fail_count = self._fail_count
            last_error = str(self._last_error) if self._last_error else None
            poll_count = self._poll_count

        return {
            "name": self._name,
            "interval": self.INTERVAL,
            "is_running": self.is_running,
            "poll_count": poll_count,
            "consecutive_failures": fail_count,
            "last_error": last_error,
        }

    def __repr__(self) -> str:  # pragma: no cover
        status = "running" if self.is_running else "stopped"
        return f"<{self.__class__.__name__} name={self._name!r} {status}>"
