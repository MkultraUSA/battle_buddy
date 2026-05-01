import os

import pytest
import requests

BASE = os.environ.get("SMOKE_TEST_BASE_URL", "")
pytestmark = pytest.mark.skipif(not BASE, reason="SMOKE_TEST_BASE_URL not set")

ENDPOINTS = [
    ("/",               200, "Battle Buddy"),
    ("/public",         200, "Battle Buddy"),
    ("/public/feed",    200, "Battle Buddy"),
    ("/public/about",   200, "Battle Buddy"),
    ("/public/homicides", 200, "Battle Buddy"),
    ("/public/aircraft", 200, "Battle Buddy"),
    ("/tip",            200, "tip"),
    ("/public/feed.rss", 200, "<?xml"),
    ("/api/homicides",  200, "homicides"),
    ("/api/incidents",  200, None),
    ("/api/stats",      200, None),
]

@pytest.mark.parametrize("path,expected_status,expected_content", ENDPOINTS)
def test_endpoint(path, expected_status, expected_content):
    r = requests.get(f"{BASE}{path}", timeout=15, allow_redirects=True)
    assert r.status_code == expected_status, f"{path} returned {r.status_code}"
    if expected_content:
        assert expected_content in r.text, f"{path} missing expected content: {expected_content!r}"
