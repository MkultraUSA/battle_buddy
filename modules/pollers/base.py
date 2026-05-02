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
from modules.talk import _post_to_talk
from modules.config import _state


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
        
        failure_count = 0
        while not self.stop_event.is_set():
            try:
                self.run()
                failure_count = 0  # Reset on success
            except Exception as e:
                failure_count += 1
                backoff_time = self.interval * (2 ** min(failure_count, 4))  # Exponential backoff up to 16x
                print(f"Poller error in {self.__class__.__name__}: {e}. Retrying in {backoff_time}s (attempt {failure_count})")
                
                if failure_count > 5: # Notify after 5 consecutive failures
                    self._send_failure_notification(e, failure_count)

                self.stop_event.wait(backoff_time)
                continue

            self.stop_event.wait(self.interval)

    def _send_failure_notification(self, e: Exception, failure_count: int):
        if not _state.get("TALK_ENABLED"):
            return
        
        message = f"Poller {self.__class__.__name__} has failed {failure_count} consecutive times. Last error: {e}"
        
        for room in _state.get("TALK_ADMIN_ROOMS", []):
            _post_to_talk(
                message,
                [room],
                _state.get("TALK_BASE_URL"),
                _state.get("TALK_USER"),
                _state.get("TALK_PASSWORD"),
                log_tag="poller-monitor"
            )

    @abc.abstractmethod
    def run(self) -> None:  # pragma: no cover
        """Perform one poll cycle. Called by _loop every `interval` seconds."""
