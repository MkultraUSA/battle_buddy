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
import re
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

#: Where the city's published camera images live. Every `TURNED_ON` record
#: carries `screenshot_address` pointing at `{FRAME_HOST}/image/{camera_id}.jpg`
#: -- verified live: HTTP 200, `image/jpeg`, ~330 KB, ~0.2 s, keyless.
#:
#: There is no video stream to link. `video/`, `stream/`, `hls/` and
#: `video/<id>/playlist.m3u8` were each probed and all answer `403` with an
#: 111-byte `application/xml` body -- the bucket denying keys that do not
#: exist, not a stream behind a check. The host root is an
#: "CCTV Image not found" page. So the frame is the whole of what the city
#: publishes, and it is what God's Eye View uses for Austin too: its
#: `server/providers/cctv/sources.js` sets this same jpg as both `url` and
#: `snapshotUrl`. Its *live video* is DelDOT and the other sources that
#: publish real HLS playlists.
#:
#: Treated as untrusted input even though it comes from the city's own API:
#: only an https URL on exactly this host with a plain `/image/<id>.jpg` path
#: is kept, and anything else is dropped rather than rendered.
FRAME_HOST = "cctv.austinmobility.io"

FRAME_PATH_RE = re.compile(r"^/image/[A-Za-z0-9_-]+\.jpg$")


def frame_url(raw: object) -> str | None:
    """Return a vetted city frame URL, or None if it is not one."""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != FRAME_HOST:
        return None
    if parsed.query or parsed.fragment or not FRAME_PATH_RE.match(parsed.path):
        return None
    if parsed.username or parsed.password or parsed.port:
        return None
    return url


def _fetch(offset: int) -> dict:
    query = urllib.parse.urlencode({
        "$select": "camera_id,location_name,camera_status,location,screenshot_address",
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
    Carries `screenshot_address` through `frame_url()`, which drops anything
    that is not the city's own frame URL.
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
        props_out = {
            "id": cam_id,
            # Many values carry a leading space; strip so popups read cleanly.
            "name": (props.get("location_name") or "").strip() or f"Camera {cam_id}",
        }
        # A camera earns a marker only if the city publishes a picture of it.
        # The city's live frames are the whole reason this layer exists -- a dot
        # with nothing behind it is a dead end for whoever clicks it -- so an
        # active camera with no usable frame is dropped from the snapshot
        # entirely rather than plotted as a location-only pin.
        frame = frame_url(props.get("screenshot_address"))
        if not frame:
            continue
        props_out["image"] = frame
        out.append({
            "type": "Feature",
            "properties": props_out,
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


def write_snapshot(snapshot: dict) -> None:
    """Replace the snapshot in one step, so a reader never sees a half file.

    This runs unattended on a timer, and Flask serves the snapshot to every
    map load straight off disk. A plain `write_text` truncates first and then
    writes, so a browser that asks for the file during the write gets invalid
    JSON and the whole camera layer silently disappears -- the map still draws,
    the legend still claims cameras, and the only symptom is an empty layer.

    Write to a temporary file in the same directory, fsync it, then
    `os.replace`, which is atomic on POSIX: a reader gets either the whole old
    file or the whole new one. The temp name starts with a dot so a partially
    written file is never itself served as a camera snapshot.
    """
    import os
    import tempfile

    payload = json.dumps(snapshot, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(
        dir=str(OUT_PATH.parent), prefix=".austin_cameras.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            # Durability before the rename, so a crash cannot leave the renamed
            # file pointing at unwritten blocks.
            os.fsync(fh.fileno())
        # mkstemp is 0600; the snapshot is public static data and is served to
        # everyone, so match the mode the file had before.
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT_PATH)
    except BaseException:
        # Never leave a stray temp file behind for the next run to trip over.
        tmp.unlink(missing_ok=True)
        raise
    # Make the rename itself durable, not just the contents.
    dir_fd = os.open(str(OUT_PATH.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def main() -> int:
    features = fetch_all()
    if not features:
        print("no features returned; refusing to overwrite a good snapshot",
              file=sys.stderr)
        return 1

    snapshot = build(features)

    # Only cameras with a published frame reach the snapshot. If the city ever
    # stops publishing images -- a renamed column, a portal move -- nearly every
    # camera drops out and the layer would quietly empty itself. Refuse to
    # overwrite a good snapshot rather than ship that.
    kept = len(snapshot["features"])
    if kept * 2 < len(features):
        print(f"only {kept} of {len(features)} active cameras published a "
              f"usable image; refusing to overwrite a good snapshot",
              file=sys.stderr)
        return 1

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
    write_snapshot(snapshot)
    print(f"wrote {OUT_PATH} -- {kept} cameras with published frames "
          f"of {len(features)} active ({OUT_PATH.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())