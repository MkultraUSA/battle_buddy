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
from modules.talk_post import post_to_talk  # noqa: F401
