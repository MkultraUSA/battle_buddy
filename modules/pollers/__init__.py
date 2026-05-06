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
  - ADSBAirAssetPoller — replacement for adsb_air_asset_thread
                         (modules/pollers/impl/adsb_air_asset.py)
  - APDNewsPoller      — replacement for apd_news_thread
                         (modules/pollers/impl/apd_news.py)
  - APDCADPoller       — replacement for apd_cad_thread
                         (modules/pollers/impl/apd_cad.py)
  - ATXFloodsPoller    — replacement for atxfloods_thread
                         (modules/pollers/impl/atxfloods.py)
  - AustinEventsPoller — replacement for austin_events_thread
                         (modules/pollers/impl/austin_events.py)
  - RedditIntelPoller  — replacement for reddit_intel_thread
                         (modules/pollers/impl/reddit_intel.py)
  - TrafficOpenDataPoller — replacement for traffic_open_data_thread
                            (modules/pollers/impl/traffic_open_data.py)

And backward-compatibility shims:
  - afd_open_data_thread — deprecated; prefer AFDOpenDataPoller().start()
  - adsb_air_asset_thread — deprecated; prefer ADSBAirAssetPoller().start()
  - apd_news_thread      — deprecated; prefer APDNewsPoller().start()
  - apd_cad_thread       — deprecated; prefer APDCADPoller().start()
  - atxfloods_thread     — deprecated; prefer ATXFloodsPoller().start()
  - austin_events_thread — deprecated; prefer AustinEventsPoller().start()
  - reddit_intel_thread  — deprecated; prefer RedditIntelPoller().start()
  - traffic_open_data_thread — deprecated; prefer TrafficOpenDataPoller().start()
"""

# Re-export everything from the legacy monolith (all remaining poller threads,
# helpers, and shared state that hasn't been extracted yet)
# Concrete BasePoller subclasses
from modules.pi_watchdog import (  # noqa: F401
    PiWatchdogService,
    _pi_command_queue,
    _pi_watchdog_alert,
    pi_watchdog_thread,
)
from modules.pollers.impl.adsb_air_asset import ADSBAirAssetPoller  # noqa: F401
from modules.pollers.impl.afd_news import AFDOpenDataPoller  # noqa: F401
from modules.pollers.impl.apd_cad import APDCADPoller  # noqa: F401
from modules.pollers.impl.apd_news import APDNewsPoller  # noqa: F401
from modules.pollers.impl.atxfloods import ATXFloodsPoller  # noqa: F401
from modules.pollers.impl.austin_events import AustinEventsPoller  # noqa: F401
from modules.pollers.impl.reddit_intel import RedditIntelPoller  # noqa: F401
from modules.pollers.impl.traffic_open_data import TrafficOpenDataPoller  # noqa: F401
from modules.pollers_legacy import *  # noqa: F401, F403
from modules.pollers_legacy import send_dm_alert  # noqa: F401


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


def adsb_air_asset_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts ADSBAirAssetPoller and blocks forever.

    .. deprecated::
        Call ``ADSBAirAssetPoller().start()`` directly instead of wrapping
        this function in a ``threading.Thread``. This shim exists only to
        avoid breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "adsb_air_asset_thread() is deprecated; use ADSBAirAssetPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = ADSBAirAssetPoller()
    poller.start()
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


def apd_cad_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts APDCADPoller and blocks forever.

    .. deprecated::
        Call ``APDCADPoller().start()`` directly instead of wrapping this
        function in a ``threading.Thread``.  This shim exists only to avoid
        breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "apd_cad_thread() is deprecated; use APDCADPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = APDCADPoller()
    poller.start()
    while not poller.stop_event.is_set():
        time.sleep(60)


def atxfloods_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts ATXFloodsPoller and blocks forever.

    .. deprecated::
        Call ``ATXFloodsPoller().start()`` directly instead of wrapping this
        function in a ``threading.Thread``.  This shim exists only to avoid
        breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "atxfloods_thread() is deprecated; use ATXFloodsPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = ATXFloodsPoller()
    poller.start()
    while not poller.stop_event.is_set():
        time.sleep(60)


def austin_events_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts AustinEventsPoller and blocks forever.

    .. deprecated::
        Call ``AustinEventsPoller().start()`` directly instead of wrapping this
        function in a ``threading.Thread``.  This shim exists only to avoid
        breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "austin_events_thread() is deprecated; use AustinEventsPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = AustinEventsPoller()
    poller.start()
    while not poller.stop_event.is_set():
        time.sleep(60)


def traffic_open_data_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts TrafficOpenDataPoller and blocks forever.

    .. deprecated::
        Call ``TrafficOpenDataPoller().start()`` directly instead of wrapping
        this function in a ``threading.Thread``.  This shim exists only to
        avoid breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "traffic_open_data_thread() is deprecated; use TrafficOpenDataPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = TrafficOpenDataPoller()
    poller.start()
    while not poller.stop_event.is_set():
        time.sleep(60)


def reddit_intel_thread() -> None:  # noqa: D401
    """Backward-compat shim — starts RedditIntelPoller and blocks forever.

    .. deprecated::
        Call ``RedditIntelPoller().start()`` directly instead of wrapping this
        function in a ``threading.Thread``.  This shim exists only to avoid
        breaking callers that still use the old thread-function pattern.
    """
    import warnings
    warnings.warn(
        "reddit_intel_thread() is deprecated; use RedditIntelPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import time
    poller = RedditIntelPoller()
    poller.start()
    while not poller.stop_event.is_set():
        time.sleep(60)
