"""Tests for the feeder-side ADSB.lol relay."""

from __future__ import annotations

import unittest
from unittest import mock

from pi import adsb_lol_relay


class ADSBRelayTests(unittest.TestCase):
    @mock.patch.object(adsb_lol_relay, "_request_json")
    def test_fetch_snapshot_filters_aircraft_without_positions(self, request_json):
        request_json.return_value = {
            "now": 1234,
            "ac": [
                {"hex": "abc123", "lat": 30.2, "lon": -97.7},
                {"hex": "no-position"},
            ],
        }

        snapshot, source_count = adsb_lol_relay.fetch_snapshot("https://source.test")

        self.assertEqual(source_count, 2)
        self.assertEqual(snapshot["now"], 1234)
        self.assertEqual(len(snapshot["aircraft"]), 1)

    @mock.patch.object(adsb_lol_relay, "_request_json")
    def test_post_snapshot_uses_bearer_token(self, request_json):
        request_json.return_value = {"status": "ok", "aircraft": 1}

        accepted = adsb_lol_relay.post_snapshot(
            "https://battlebuddy.test/api/adsb/ingest",
            "secret-token",
            {"now": 1234, "aircraft": []},
        )

        self.assertEqual(accepted, 1)
        request = request_json.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")


if __name__ == "__main__":
    unittest.main()
