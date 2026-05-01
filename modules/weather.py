from datetime import datetime

import pytz
import requests


def _get_nws_weather(lat, lon):
    """
    Fetch weather from NWS API. Returns a dict of current conditions and a 5-day
    forecast, or None on error.
    """
    try:
        # NWS requires a two-stage request: first get the gridpoint URL for the lat/lon
        # then use that to get the actual forecast.
        headers = {"User-Agent": "BattleBuddy/1.0 (battlebuddy.com, battlebuddystatus@gmail.com)"}
        
        # 1. Get gridpoint URL and cache it
        grid_url = None
        # Simple file-based cache for grid URLs to avoid repeated lookups
        cache_file = f"/tmp/nws_grid_{lat:.4f}_{lon:.4f}.txt"
        try:
            with open(cache_file, "r") as f:
                grid_url = f.read().strip()
        except FileNotFoundError:
            pass

        if not grid_url:
            points_url = f"https://api.weather.gov/points/{lat},{lon}"
            r = requests.get(points_url, headers=headers, timeout=5)
            r.raise_for_status()
            grid_url = r.json()["properties"]["forecastGridData"]
            with open(cache_file, "w") as f:
                f.write(grid_url)

        # 2. Get forecast from gridpoint URL
        r = requests.get(grid_url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        # NWS data is a complex beast. We need to parse temperature, wind, and
        # precipitation chance for both current conditions and the forecast.
        
        # Current conditions
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
        temp_valid = _find_closest_value(data["properties"]["temperature"]["values"], now_utc)
        wind_valid = _find_closest_value(data["properties"]["windSpeed"]["values"], now_utc)
        desc_valid = _find_closest_value(data["properties"]["weather"]["values"], now_utc, is_weather=True)

        current = {
            "temp": round(temp_valid["value"] * 9/5 + 32) if temp_valid else None,
            "unit": "F",
            "wind": f"{wind_valid['value']:.0f} km/h" if wind_valid else "",
            "desc": desc_valid["value"][0]["weather"] if desc_valid and desc_valid["value"] else ""
        }

        # Daily forecast (next 5 days)
        # NWS provides min/max temps and precip chance over 12-hour periods. We need to
        # aggregate these into daily forecasts.
        min_temps = data["properties"]["minTemperature"]["values"]
        max_temps = data["properties"]["maxTemperature"]["values"]
        precip    = data["properties"]["probabilityOfPrecipitation"]["values"]
        shortdesc = data["properties"]["skyCover"]["values"] # Using sky cover as a proxy for a short description
        
        forecast = _parse_nws_forecast(min_temps, max_temps, precip, shortdesc, now_utc.astimezone(pytz.timezone('US/Central')))

        return {"current": current, "forecast": forecast}

    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"[weather] NWS API error: {e}", flush=True)
        return None

def _find_closest_value(values, target_dt, is_weather=False):
    """
    Find the value entry in a NWS time series that is closest to the target datetime.
    NWS time series are lists of {"validTime": "...", "value": ...}.
    The validTime can be a single timestamp or an ISO 8601 duration like P2DT3H.
    For weather, we only care about single timestamps.
    """
    best_match = None
    min_delta = float('inf')

    for v in values:
        try:
            vt_str = v["validTime"]
            if '/' in vt_str: # It's a duration, not a point-in-time value
                 # Split the duration string and take the start time
                start_str = vt_str.split('/')[0]
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            else: # It's a single time
                dt = datetime.fromisoformat(vt_str.replace("Z", "+00:00"))

            delta = abs((dt - target_dt).total_seconds())

            if is_weather:
                # For weather descriptions, we want the most recent *past* value
                if dt <= target_dt and (target_dt - dt).total_seconds() < min_delta:
                    min_delta = (target_dt - dt).total_seconds()
                    best_match = v
            else:
                # For other values, find the absolute closest time
                if delta < min_delta:
                    min_delta = delta
                    best_match = v
        except (ValueError, KeyError):
            # Skip entries with invalid time formats or missing keys
            continue
            
    return best_match

def _parse_nws_forecast(min_temps, max_temps, precip, sky_cover, now_local):
    """
    Helper to combine NWS 12-hour forecast periods into daily summaries.
    This is complex because min/max temps can have different timestamps and we
    need to correctly associate them with the right day.
    """
    by_day = {} # YYYY-MM-DD -> {temps: [], precips: [], ...}

    # Use a set to track which day each temp belongs to
    all_series = [
        (min_temps, "min_temp"),
        (max_temps, "max_temp"),
        (precip, "precip"),
        (sky_cover, "sky")
    ]

    for series, key in all_series:
        for val in series:
            try:
                start_str = val["validTime"].split('/')[0]
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(now_local.tzinfo)
                
                # NWS daily boundaries can be weird (e.g. 6am-6pm). We'll snap to calendar day.
                day_str = start_dt.strftime("%Y-%m-%d")

                if day_str not in by_day:
                    by_day[day_str] = {"min_temps": [], "max_temps": [], "precips": [], "skies": []}
                
                if key == "min_temp" or key == "max_temp":
                     by_day[day_str][key+"s"].append(round(val["value"] * 9/5 + 32))
                elif key == "precip":
                    by_day[day_str]["precips"].append(val["value"] or 0)
                elif key == "sky":
                    # Map sky cover % to a short description
                     # Find the single sky_cover value within the period
                    sky_value = val["value"]
                    if sky_value <= 10:   short = "Clear"
                    elif sky_value <= 30: short = "Mostly Clear"
                    elif sky_value <= 70: short = "Partly Cloudy"
                    elif sky_value <= 90: short = "Mostly Cloudy"
                    else: short = "Cloudy"
                    by_day[day_str]["skies"].append(short)

            except (ValueError, KeyError, IndexError):
                continue
    
    # Now, synthesize the daily forecasts
    forecast_days = []
    # Sort days chronologically
    sorted_days = sorted(by_day.keys())

    for day_str in sorted_days:
        day_data = by_day[day_str]
        
        # Skip today if it's already evening.
        day_dt = datetime.strptime(day_str, "%Y-%m-%d").date()
        if day_dt == now_local.date() and now_local.hour > 18:
            continue
            
        # If a day starts after today, use its date. If it's today, label it "Tonight" or "Today"
        if day_dt > now_local.date():
            name = day_dt.strftime("%a")
        elif now_local.hour >= 18:
             name = "Tonight"
        else:
            name = "Today"
        
        # We might have multiple min/max temps for a day. Usually NWS gives one of each.
        # Let's take the highest max and lowest min.
        high_temp = max(day_data["max_temps"]) if day_data["max_temps"] else None
        low_temp = min(day_data["min_temps"]) if day_data["min_temps"] else None

        # Determine the single temp to show. If it's today, show high. For future days, show high. For "Tonight", show low.
        display_temp = None
        if name == "Tonight":
            display_temp = low_temp if low_temp is not None else high_temp
        else:
            display_temp = high_temp if high_temp is not None else low_temp
            
        # Skip if we couldn't determine a temperature
        if display_temp is None:
            continue

        forecast_days.append({
            "name": name,
            "temp": display_temp,
            "short": day_data["skies"][0] if day_data["skies"] else "",
            "precip": max(day_data["precips"]) if day_data["precips"] else 0,
        })
        
        if len(forecast_days) >= 5:
            break
            
    return forecast_days
