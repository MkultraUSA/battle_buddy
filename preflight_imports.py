#!/usr/bin/env python3
"""Pre-flight import validation for Battle Buddy.
Run before starting the service to catch ImportError bugs that would otherwise
cause silent runtime failures. Exit 1 if any import fails.
"""
import sys
import traceback

FAILURES = []

def check_import(module_path: str, names: list[str]):
    """Try importing names from module_path. Record failures."""
    try:
        mod = __import__(module_path, fromlist=names)
        for name in names:
            try:
                getattr(mod, name)
            except AttributeError:
                FAILURES.append(f"{module_path}.{name} — AttributeError: not found in module")
    except ImportError as e:
        FAILURES.append(f"{module_path} — ImportError: {e}")

# Critical imports — if any of these fail, the service WILL malfunction silently
CHECKS = [
    # incident_engine.py — crash on every incident creation
    ("modules.alerts", ["create_deck_card", "post_banner", "send_dm_alert", "clear_banner"]),
    # audio_receiver.py /api/adsb endpoint
    ("modules.pollers.impl.adsb_air_asset", ["ADSB_TRAIL_SECS", "KNOWN_AIR_ASSETS", "ADSBAirAssetPoller"]),
    # pollers/__init__.py — re-exports used by audio_receiver and poller impls
    ("modules.pollers", ["send_dm_alert", "ADSB_TRAIL_SECS", "ADSBAirAssetPoller"]),
    # incident_engine infra
    ("modules.atak", ["_atak_post_marker", "_atak_clear_marker"]),
    ("modules.incident_engine", ["analyze_for_incident", "_create_incident"]),
    # poller implementations
    ("modules.pollers.impl.apd_news", ["APDNewsPoller"]),
    ("modules.pollers.impl.adsb_air_asset", ["ADSBAirAssetPoller"]),
]

def main():
    for module_path, names in CHECKS:
        check_import(module_path, names)

    if FAILURES:
        print(f"PREFLIGHT FAILED: {len(FAILURES)} import errors detected:")
        for f in FAILURES:
            print(f"  {f}")
        print("\nThese failures will cause silent runtime crashes. Fix before starting.")
        sys.exit(1)
    else:
        print("PREFLIGHT OK: All critical imports validated.")
        sys.exit(0)

if __name__ == "__main__":
    main()
