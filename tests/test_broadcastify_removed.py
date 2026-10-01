"""Broadcastify is gone, and should stay gone.

The A/B result, from 30 days of production data rather than a two-hour sample:

  * 31 incidents had the Broadcastify stream as their ONLY source.
  * 14 of those were HIGH severity -- SHOOTING, STABBING, MASS CASUALTY,
    STRUCTURE FIRE -- from a single untagged mixed-audio feed (tgid=0, tag
    "Austin/Travis Scanner") with zero corroborating OP25 traffic.

  * Per call it produced 0.70% incidents against the radio desk's 3.77%, 33.7%
    of its transcripts were untranscribable, and it never once carried a
    talkgroup identity, so nothing it produced could be attributed to a channel.

  * Time-of-day matched A/B (same 2-hour window, stream on vs off): OP25 calls
    were unchanged (78 vs 81), so it was not buying detection. Only 2 incidents
    in the on-window involved it at all.

For a product whose value is trustworthy public-safety information, an
uncorroborated "gunshot detected" or "MCI" is a credibility problem no amount of
Whisper accuracy fixes. Removed 2026-10-01 by owner decision.

The ingest semaphore in `receive()` is deliberately KEPT: it is generic
(`node != "pi5"`) and provides backpressure for any future secondary source.
What must not come back is this particular feed.

Historical rows are NOT deleted -- `calls` and `incidents` keep node='broadcastify'
history, per the project's standing rule that nothing is ever removed.
"""

from __future__ import annotations

import pathlib
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


class TestBroadcastifyIsGone(unittest.TestCase):
    def test_the_recorder_no_longer_exists(self):
        self.assertFalse(
            (_ROOT / "stream_recorder.py").exists(),
            "stream_recorder.py was removed; restoring it would restore an "
            "uncorroborated high-severity false-positive source",
        )

    def test_no_live_broadcastify_upstream_is_referenced(self):
        """A hardcoded feed URL is how it would quietly come back."""
        this_file = pathlib.Path(__file__).resolve()
        offenders = []
        for path in _ROOT.rglob("*.py"):
            if any(p in {".git", "venv", "__pycache__"} for p in path.parts):
                continue
            if path.resolve() == this_file:
                continue  # this test names the needles on purpose
            text = path.read_text(errors="replace")
            for needle in ("broadcastify.cdnstream", "STREAM_USER", "STREAM_PASS"):
                if needle in text:
                    offenders.append(f"{path.relative_to(_ROOT)}: {needle}")
        self.assertEqual(
            offenders, [],
            "Broadcastify credentials or feed URL are back in the tree: "
            + ", ".join(offenders),
        )


class TestIngestSemaphoreIsStillGeneral(unittest.TestCase):
    """The backpressure must survive -- it is not Broadcastify-specific."""

    def test_receive_still_branches_on_source_node(self):
        src = (_ROOT / "audio_receiver.py").read_text()
        self.assertIn(
            'is_broadcastify = node != "pi5"', src,
            "the per-source ingest backpressure was removed along with the feed; "
            "it is generic and should still apply to any secondary source",
        )


if __name__ == "__main__":
    unittest.main()