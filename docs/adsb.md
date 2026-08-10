# ADS-B Aircraft Pipeline

Battle Buddy keeps aircraft tracking separate from audio ingestion and
transcription. The server-side routes and aircraft page live in
`modules/aircraft.py`; the feeder relay lives in `pi/adsb_lol_relay.py`.

## Data flow

1. A registered ADSB.lol feeder Pi requests the feeder-only re-api.
2. The Pi relay posts the snapshot to `/api/adsb/ingest` using a bearer token.
3. Battle Buddy validates, bounds, and sanitizes the aircraft fields.
4. `/api/adsb/live` serves the latest network snapshot.
5. `/api/adsb` continues to serve locally persisted helicopter trails.
6. `/public/aircraft` combines both sources on the dedicated aircraft map.

The incident and homicide maps do not load aircraft data.

## Server configuration

Generate a strong random token and add it to the Battle Buddy environment:

```ini
BB_ADSB_INGEST_TOKEN=replace_with_random_token
```

Restart Battle Buddy after changing its environment.

## Feeder Pi configuration

Install the relay and service:

```bash
sudo install -m 0755 pi/adsb_lol_relay.py /usr/local/bin/bb-adsb-bridge.py
sudo install -m 0644 pi/bb-adsb-bridge.service /etc/systemd/system/bb-adsb-bridge.service
```

Create `/etc/default/bb-adsb-bridge`:

```ini
BB_ADSB_INGEST_URL=https://battlebuddy.example.com/api/adsb/ingest
BB_ADSB_INGEST_TOKEN=replace_with_the_same_random_token
BB_ADSB_SOURCE_URL=https://re-api.adsb.lol?circle=30.2672,-97.7431,52
BB_ADSB_RELAY_INTERVAL_SECONDS=30
```

Protect the environment file and start the relay:

```bash
sudo chmod 600 /etc/default/bb-adsb-bridge
sudo systemctl daemon-reload
sudo systemctl enable --now bb-adsb-bridge.service
```

Verify the relay without printing its token:

```bash
systemctl is-active bb-adsb-bridge.service
journalctl -u bb-adsb-bridge.service -n 20 --no-pager
curl -fsS https://battlebuddy.example.com/api/adsb/live
```

ADSB.lol data is attributed under ODbL 1.0 on the aircraft page.
