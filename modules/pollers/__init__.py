"""
modules/pollers/__init__.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Thin compatibility shim for the pollers package.

Re-exports everything from modules._pollers_legacy (the original monolithic
pollers.py content) so that ``from modules.pollers import *`` and any explicit
imports continue to work without modification in audio_receiver.py.

Also exports:
  - AFDOpenDataPoller  — the new BasePoller-based replacement for
                         afd_open_data_thread (modules/pollers/impl/afd_news.py)
  - afd_open_data_thread — backward-compatibility shim that starts an
                           AFDOpenDataPoller instance (deprecated; prefer
                           AFDOpenDataPoller().start() directly).
"""

# Re-export everything from the legacy monolith (all remaining poller threads,
# helpers, and shared state that hasn't been extracted yet)
from modules._pollers_legacy import *  # noqa: F401, F403

# Concrete BasePoller subclass — AFD Open Data incidents
from modules.pollers.impl.afd_news import AFDOpenDataPoller  # noqa: F401


def afd_open_data_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts AFDOpenDataPoller and blocks forever.

    .. deprecated::
        Call ``AFDOpenDataPoller().start()`` directly instead of wrapping this
        function in a ``threading.Thread``.  This shim exists only to avoid
        breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "afd_open_data_thread() is deprecated; use AFDOpenDataPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = AFDOpenDataPoller()
    poller.start()
    # Block this thread indefinitely so the caller's daemon thread stays alive
    while not poller.stop_event.is_set():
        time.sleep(60)


__all__ = [
    # Concrete BasePoller subclasses
    "AFDOpenDataPoller",
    # Backward-compat shim
    "afd_open_data_thread",
]
