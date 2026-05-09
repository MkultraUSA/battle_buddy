import json
import time
import urllib.request

_CACHE = {"data": None, "ts": 0}
_TTL_SECONDS = 900  # 15 minutes


def get_space_weather():
    now = time.time()
    if _CACHE["data"] and (now - _CACHE["ts"] < _TTL_SECONDS):
        return _CACHE["data"]

    headers = {"User-Agent": "BattleBuddy/2.0 (ops@battlebuddy.news)"}
    try:
        kp_req = urllib.request.Request(
            "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json",
            headers=headers,
        )
        with urllib.request.urlopen(kp_req, timeout=8) as r:
            kp_rows = json.loads(r.read())
        latest_kp = kp_rows[-1] if kp_rows else {}

        xray_req = urllib.request.Request(
            "https://services.swpc.noaa.gov/json/goes/primary/xrays-7-day.json",
            headers=headers,
        )
        with urllib.request.urlopen(xray_req, timeout=8) as r:
            xray_rows = json.loads(r.read())
        xray_long = [x for x in xray_rows if x.get("energy") == "0.1-0.8nm"]
        latest_xray = xray_long[-1] if xray_long else (xray_rows[-1] if xray_rows else {})

        sw_req = urllib.request.Request(
            "https://services.swpc.noaa.gov/products/solar-wind/plasma-7-day.json",
            headers=headers,
        )
        with urllib.request.urlopen(sw_req, timeout=8) as r:
            sw_rows = json.loads(r.read())
        sw_latest = {}
        if isinstance(sw_rows, list) and len(sw_rows) >= 2:
            hdr = sw_rows[0]
            row = sw_rows[-1]
            if isinstance(hdr, list) and isinstance(row, list) and len(hdr) == len(row):
                sw_latest = dict(zip(hdr, row))

        data = {
            "kp_index": latest_kp.get("kp_index"),
            "kp_time": latest_kp.get("time_tag"),
            "xray_flux": latest_xray.get("flux"),
            "xray_time": latest_xray.get("time_tag"),
            "solar_wind_speed_km_s": sw_latest.get("speed"),
            "solar_wind_density_p_cm3": sw_latest.get("density"),
            "solar_wind_temp_k": sw_latest.get("temperature"),
            "solar_wind_time": sw_latest.get("time_tag"),
            "source": "NOAA SWPC",
            "stale": False,
            "cache_ts": now,
        }
        _CACHE["data"] = data
        _CACHE["ts"] = now
        return data
    except Exception as e:
        print(f"[space-weather] fetch error: {e}", flush=True)
        if _CACHE["data"]:
            stale_data = dict(_CACHE["data"])
            stale_data["stale"] = True
            stale_data["stale_reason"] = str(e)
            stale_data["cache_age_seconds"] = int(now - _CACHE["ts"])
            return stale_data
        return None
