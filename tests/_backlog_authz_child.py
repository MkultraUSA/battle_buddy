"""Scenario runner for tests/test_backlog_authz.py.

Not a test file. Executed as a subprocess by that suite so each scenario runs in
a clean interpreter, for the same reason as _receive_auth_child.py: importing
audio_receiver pulls in faster_whisper via modules.transcription, which other
suites stub in sys.modules at collection time and never remove, so a stub would
mean the route under test was not the real one.

This harness exists because the AST tests in test_backlog_authz.py verified the
shape of the wiring and let three real defects through to production:

  1. `if checked is None: return checked` -- inverted, since the guard returns
     None on SUCCESS. Unauthenticated callers had their refusal ignored and got
     200; valid callers got 500.
  2. The guard ended in `return data`, so Flask jsonified the parsed request
     body and echoed the caller's token back in the response.
  3. A later assertion checked `Return.value is None`, but `return None`
     parses to Constant(None), so it failed against correct code.

All three were found by probing the deployed endpoints, not by the suite. These
assertions execute the routes instead.

Unlike _receive_auth_child.py, the token is set BEFORE import: `_backlog_token`
is a module-level global read at import time (audio_receiver.py:160), not per
request, so setting it afterwards would not take effect.

Usage: python _backlog_authz_child.py <scenario.json> <result.json>

The result goes to a file because the app logs `[backlog] ...` lines to stdout
while the scenario runs. Exits non-zero with the traceback on stderr if the
scenario itself raises, so a broken scenario can never look like a pass.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> None:
    scenario_path, result_path = sys.argv[1], sys.argv[2]
    scenario = json.loads(Path(scenario_path).read_text())

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "calls.db")

        # The whole config layer must point at the temp dir BEFORE audio_receiver
        # imports, since modules.config freezes these at import time.
        os.environ["DB_PATH"] = db_path
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
        os.environ["TIPS_UPLOAD_DIR"] = str(Path(tmp) / "tips")
        os.environ["TGID_TSV"] = str(Path(tmp) / "no-such-tags.tsv")

        # The backlog is file-backed now, and raw_audio_queue reads its root at
        # import time. Without this the child would seed, claim and delete items
        # in the REAL queue directory -- on the VPS that is
        # /opt/battlebuddy/raw_audio_queue. Point it inside the temp dir.
        #
        # A caller may supply queue_dir to share one store across two child runs,
        # which is how the restart-survival test proves durability: two
        # interpreters, one queue directory.
        _qdir = scenario.get("queue_dir")
        os.environ["BB_RAW_AUDIO_QUEUE_DIR"] = (
            str(_qdir) if _qdir else str(Path(tmp) / "raw_audio_queue")
        )

        token = scenario.get("token")
        if token is None:
            os.environ.pop("BB_BACKLOG_AGENT_TOKEN", None)
        else:
            os.environ["BB_BACKLOG_AGENT_TOKEN"] = token

        import audio_receiver
        from modules.database import init_db

        init_db()

        # Seed one real durable queue item so `claim` has work to hand out and
        # so the refusal cases prove the store was neither drained nor deleted.
        from modules.raw_audio_queue import enqueue_raw_audio

        _skip_seed = scenario.get("skip_seed")
        seeded_id = scenario.get("seeded_id") or ""
        if not _skip_seed:
            # Coordinates are part of what enqueue captures, so the seed must
            # carry some -- otherwise "the stored default is preserved" is not
            # observable and a mutation that discards it passes silently.
            seeded_id = enqueue_raw_audio(
                ts=1.0, tgid=12345, tag="seeding", category="Test", node="pie3",
                duration=1.0, wav_bytes=b"HELLO",
                default_lat=30.2672, default_lon=-97.7431,
            )

        client = audio_receiver.app.test_client()
        headers = {}
        for name, value in (scenario.get("headers") or {}).items():
            if value is not None:
                headers[name] = value

        # The success path of /complete runs llm_analyze (spends an LLM call),
        # analyze_for_incident (can file an incident) and post_to_talk (pushes to
        # subscribers). Stub those so a test can drive a real processed result
        # without network egress, while insert_call still writes a genuine row.
        incident_call = {}
        if scenario.get("stub_side_effects"):
            from unittest import mock as _mock

            audio_receiver.llm_analyze = _mock.MagicMock(return_value=None)
            _analyze = _mock.MagicMock(return_value=None)

            def _capture(call, *_a, **_kw):
                incident_call.update(call if isinstance(call, dict) else {})
                return None

            _analyze.side_effect = _capture
            audio_receiver.analyze_for_incident = _analyze
            audio_receiver.post_to_talk = _mock.MagicMock(return_value=None)

        # Force the storage step to fail so the test can prove a clip is
        # retained when processing errors. Removing the clip before the
        # transcript is durably stored is silent data loss: a transient SQLite
        # lock or a Talk outage would discard audio that was already paid for.
        if scenario.get("fail_insert"):
            def _boom(*_a, **_kw):
                raise RuntimeError("simulated storage failure")
            audio_receiver.insert_call = _boom

        route = scenario["route"]
        # Let a scenario refer to the clip it just seeded. Tests cannot know the
        # generated id, and guessing one would make remove_queued_audio a no-op
        # so the assertion pass for the wrong reason.
        body = json.loads(json.dumps(scenario.get("body") or {}).replace("SEEDED_ID", seeded_id))
        response = client.post(
            f"/api/backlog/{route}",
            json=body,
            headers=headers,
        )

        rows = 0
        last_category = None
        last_lat = last_lon = None
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            _row = conn.execute(
                "SELECT category, lat, lon FROM calls ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_category = _row[0] if _row else None
            last_lat = _row[1] if _row else None
            last_lon = _row[2] if _row else None

        # Durable depth. `claim` does NOT unlink: it takes a lease and leaves the item
        # in pending, so an authorised claim still shows depth 1 here. What proves
        # the claim worked is `claimed` (the lease holder) and that the item is
        # no longer claimable by a second worker.
        from modules.raw_audio_queue import get_raw_audio_queue_counts

        counts = get_raw_audio_queue_counts()
        queued = int(counts.get("pending") or 0)

        # Does the seeded clip still exist on disk? queue_depth can hide a
        # delete-then-refuse ordering bug because a refused request that wrongly
        # removed the item still reports a clean depth.
        try:
            seed_present = (
                Path(os.environ["BB_RAW_AUDIO_QUEUE_DIR"]) / "pending" / f"{seeded_id}.json"
            ).exists()
        except Exception:
            seed_present = None

        claimed = None
        try:
            claimed = json.loads(
                (Path(os.environ["BB_RAW_AUDIO_QUEUE_DIR"]) / "pending" / f"{seeded_id}.json")
                .read_text(encoding="utf-8")
            ).get("lease_worker_id")
        except Exception:
            claimed = None

        Path(result_path).write_text(
            json.dumps(
                {
                    "status": response.status_code,
                    "raw_body": (response.get_data(as_text=True) or ""),
                    "body": response.get_json(silent=True),
                    "calls_rows": rows,
                    "queue_depth": queued,
                    "lease_worker_id": claimed,
                    "seeded_id": seeded_id,
                    "seed_present": seed_present,
                    "last_call_category": last_category,
                    "last_call_lat": last_lat,
                    "last_call_lon": last_lon,
                    "incident_category": incident_call.get("category"),
                    "incident_lat": incident_call.get("lat"),
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()