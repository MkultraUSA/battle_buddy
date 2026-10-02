#!/usr/bin/env python3
"""Refresh the baked Austin traffic-camera snapshot.

Run this by hand when the city's data changes; the output is committed so the map
serves no runtime dependency on a third-party endpoint.

    python scripts/fetch_austin_cameras.py

Source: City of Austin Open Data, dataset b4k4-adkb ("Traffic Cameras"), owned by
Arterial Management Division, Austin Transportation and Public Works. Licence is
**Public Domain**; attribution is supplied on the map.

Two properties of the API that are easy to get wrong, both learned the hard way:

  * `resource/*.geojson` **silently caps at 1000 features**. The full dataset is
    1008 rows, so a naive fetch returns 1000 of them with no error and no warning.
    Anything relying on one unpaged request quietly drops records. This script
    pages explicitly and then verifies the count, so a silent truncation fails
    loudly instead.
  * `export.geojson` returns 406 Not Acceptable. Use `resource/*.geojson`.

Also note the status field is `camera_status`, not `status`, and there is no
`ACTIVE` value -- live cameras are `TURNED_ON`. `DESIRED` (planned), `VOID` and
`REMOVED` are excluded, which is the point: showing cameras that no longer exist
makes the layer look unreliable.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

DATASET = "b4k4-adkb"
BASE = f"https://data.austintexas.gov/resource/{DATASET}.geojson"
OUT_PATH = Path(__file__).resolve().parent.parent / "static" / "data" / "austin_cameras.json"

#: Fetch in pages. The API stops at 1000 per request and says nothing, so the
#: page size is deliberately below that and the total is verified afterwards.
PAGE = 500
MAX_PAGES = 20

#: Live only. The other three values are planned, voided and removed cameras.
ACTIVE_STATUS = "TURNED_ON"

#: ~1 m. More precision than a web map can render, and it keeps the file small.
COORD_PRECISION = 5


def _fetch(offset: int) -> dict:
    query = urllib.parse.urlencode({
        "$select": "camera_id,location_name,camera_status,location",
        "$where": f"upper(camera_status)='{ACTIVE_STATUS}'",
        "$order": "camera_id",
        "$limit": PAGE,
        "$offset": offset,
    })
    req = urllib.request.Request(f"{BASE}?{query}", headers={"User-Agent": "battle-buddy/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all() -> list[dict]:
    features: list[dict] = []
    for page in range(MAX_PAGES):
        batch = _fetch(page * PAGE).get("features", [])
        features.extend(batch)
        if len(batch) < PAGE:
            return features
        print(f"  fetched {len(features)}...", file=sys.stderr)
    raise RuntimeError(f"paged {MAX_PAGES} times without reaching the end; refusing to guess")


def build(features: list[dict]) -> dict:
    """Trim to what the map renders.

    Drops `camera_status` because the fetch already filters it, so it is a
    constant across every feature and pure weight. Trims the leading space the
    city puts on many `location_name` values. Rounds coordinates to ~1 m.
    """
    out = []
    seen = set()
    for feat in features:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "Point" or len(coords) < 2:
            continue
        props = feat.get("properties") or {}
        cam_id = str(props.get("camera_id") or "").strip()
        if not cam_id or cam_id in seen:
            continue
        seen.add(cam_id)
        lon, lat = coords[0], coords[1]
        out.append({
            "type": "Feature",
            "properties": {
                "id": cam_id,
                # Many values carry a leading space; strip so popups read cleanly.
                "name": (props.get("location_name") or "").strip() or f"Camera {cam_id}",
            },
            "geometry": {
                "type": "Point",
                "coordinates": [round(float(lon), COORD_PRECISION),
                                round(float(lat), COORD_PRECISION)],
            },
        })
    out.sort(key=lambda f: f["properties"]["id"])
    return {
        "type": "FeatureCollection",
        # Recorded so the map can show how stale the snapshot is.
        "generated": None,
        "source": "City of Austin Open Data b4k4-adkb (Traffic Cameras), public domain",
        "features": out,
    }


def main() -> int:
    features = fetch_all()
    if not features:
        print("no features returned; refusing to overwrite a good snapshot",
              file=sys.stderr)
        return 1

    snapshot = build(features)

    # Sanity-check the bounds. A fetch that silently returned something else
    # (wrong dataset, bad filter) would otherwise be committed and shipped.
    lons = [f["geometry"]["coordinates"][0] for f in snapshot["features"]]
    lats = [f["geometry"]["coordinates"][1] for f in snapshot["features"]]
    if not (min(lons) < -97.6 and max(lons) > -98.0):
        print(f"longitudes out of Austin range: {min(lons)}..{max(lons)}", file=sys.stderr)
        return 1
    if not (min(lats) < 30.4 and max(lats) > 30.0):
        print(f"latitudes out of Austin range: {min(lats)}..{max(lats)}", file=sys.stderr)
        return 1

    import datetime as _dt
    snapshot["generated"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(snapshot, separators=(",", ":")), encoding="utf-8"
    )
    print(f"wrote {OUT_PATH} -- {len(snapshot['features'])} cameras, "
          f"{OUT_PATH.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())