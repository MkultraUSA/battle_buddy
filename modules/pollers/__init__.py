"""
modules/pollers/__init__.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Thin compatibility shim for the pollers package.

Re-exports everything from modules._pollers_legacy (the original monolithic
pollers.py content) so that ``from modules.pollers import *`` and any explicit
imports continue to work without modification in audio_receiver.py.

Also exports concrete BasePoller subclasses:
  - AFDOpenDataPoller  — replacement for afd_open_data_thread
                         (modules/pollers/impl/afd_news.py)
  - APDNewsPoller      — replacement for apd_news_thread
                         (modules/pollers/impl/apd_news.py)

And backward-compatibility shims:
  - afd_open_data_thread — deprecated; prefer AFDOpenDataPoller().start()
  - apd_news_thread      — deprecated; prefer APDNewsPoller().start()
"""

# Re-export everything from the legacy monolith (all remaining poller threads,
# helpers, and shared state that hasn't been extracted yet)
from modules.pollers_legacy import *  # noqa: F401, F403
from modules.pollers_legacy import _pi_command_queue  # noqa: F401

# Concrete BasePoller subclasses
from modules.pollers.impl.afd_news import AFDOpenDataPoller  # noqa: F401
from modules.pollers.impl.apd_news import APDNewsPoller      # noqa: F401


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


def apd_news_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts APDNewsPoller and blocks forever.

    .. deprecated::
        Call ``APDNewsPoller().start()`` directly instead of wrapping this
        function in a ``threading.Thread``.  This shim exists only to avoid
        breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "apd_news_thread() is deprecated; use APDNewsPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = APDNewsPoller()
    poller.start()
    # Block this thread indefinitely so the caller's daemon thread stays alive
    while not poller.stop_event.is_set():
        time.sleep(60)


__all__ = [
    # Concrete BasePoller subclasses
    "AFDOpenDataPoller",
    "APDNewsPoller",
    # Backward-compat shims
    "afd_open_data_thread",
    "apd_news_thread",
]
