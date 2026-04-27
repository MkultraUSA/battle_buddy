"""
modules/pollers/base.py
~~~~~~~~~~~~~~~~~~~~~~~
Abstract base class for all Battle Buddy background pollers.

Subclasses implement run() with their fetch/process logic.
BasePoller manages the thread lifecycle: start/stop and the poll loop.
"""

import abc
import threading
import time


class BasePoller(abc.ABC):
    """Abstract poller that runs run() in a daemon thread every `interval` seconds."""

    def __init__(self, interval: int = 300) -> None:
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
        if self.stop_event.is_set():
            return
        while True:
            try:
                self.run()
            except Exception as e:
                print("Poller error: " + str(e))
            time.sleep(self.interval)
            if self.stop_event.is_set():
                return

    @abc.abstractmethod
    def run(self) -> None:  # pragma: no cover
        """Perform one poll cycle. Called by _loop every `interval` seconds."""
