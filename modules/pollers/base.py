"""
modules/pollers/base.py
~~~~~~~~~~~~~~~~~~~~~~~
Abstract base class for all Battle Buddy background pollers.

Subclasses implement run() with their fetch/process logic.
BasePoller manages the thread lifecycle: start/stop and the poll loop.
"""

import abc
import random
import threading


class BasePoller(abc.ABC):
    """Abstract poller that runs run() in a daemon thread every `interval` seconds."""

    def __init__(self, interval: float = 300) -> None:
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        """Start the background polling thread."""
        self.thread.start()

    def stop(self) -> None:
        """Signal the loop to stop and wait for the thread to finish."""
        self.stop_event.set()
        self.thread.join()

    def _loop(self) -> None:
        consecutive_failures = 0
        backoff_delay = float(self.interval)
        if self.stop_event.is_set():
            return
        while not self.stop_event.is_set():
            try:
                self.run()
                consecutive_failures = 0
                backoff_delay = float(self.interval)
                delay = backoff_delay
            except Exception as e:
                consecutive_failures += 1
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
