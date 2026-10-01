"""Behavioural tests for the /api/backlog worker endpoints.

These assert executed behaviour, not source shape. The AST tests in this file
cover the static wiring; this class runs the real Flask app in a subprocess
(see _backlog_authz_child.py) because shape-level assertions let three genuine
defects through to production:

  * `if checked is None: return checked` -- inverted. The guard returns None on
    SUCCESS, so an unauthenticated caller had their refusal ignored and received
    200, while a valid caller got a 500. The endpoints were no more protected
    than before the guard was added.
  * The guard ended in `return data`, so Flask jsonified the parsed request body
    and echoed the caller's token back in the response.
  * An assertion checked `Return.value is None` when `return None` actually
    parses to Constant(None), failing against correct code.

The property that matters most is not the status code but what an unauthorised
caller can cause: `complete` runs insert_call -> llm_analyze ->
analyze_for_incident -> post_to_talk, so an open endpoint lets a stranger write
a call row, spend an LLM call, file an incident and post to Talk. Each refusal
case below therefore asserts the database is untouched and the queue is intact,
not merely that the response was 401.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CHILD = _HERE / "_backlog_authz_child.py"


def _audio_receiver_importable() -> tuple[bool, str]:
    """Can this interpreter really import audio_receiver?

    The suite skips rather than passes when it cannot, so an environment missing
    faster_whisper never reports a green run it did not earn. On the production
    venv this is always true, and that is the authoritative baseline. Mirrors
    test_receive_auth.py.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import audio_receiver"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    return probe.returncode == 0, (probe.stderr or "")[-500:]


_IMPORTABLE, _IMPORT_ERROR = _audio_receiver_importable()


class _BacklogAuthzCase(unittest.TestCase):
    """Base case so EVERY scenario skips together when the route is unimportable.

    Per-class decoration was wrong in test_receive_auth.py: it left two of three
    classes running, turning an environment limitation into five red tests
    rather than an honest skip.
    """

    def setUp(self) -> None:
        if not _IMPORTABLE:
            self.skipTest(
                "audio_receiver cannot be imported here (needs faster_whisper); "
                "the authoritative baseline is /opt/battlebuddy/venv on the VPS. "
                f"{_IMPORT_ERROR}"
            )

# A payload that would do real damage if it reached the handler: it inserts a
# call row, runs an LLM call, and files an incident.
_FABRICATED = {
    "item_id": "attacker-supplied",
    "tgid": 12345,
    "tag": "seeding",
    "transcript": "Structure fire, 100 block of Main St, second engine staging.",
    "node": "pie3",
    "duration": 3.0,
}


def _run(route: str, *, token=None, body=None, headers=None,
         queue_dir=None, skip_seed=False, seeded_id=None,
         stub_side_effects=False, fail_insert=False):
    """Run one scenario in a clean interpreter; returns the child's JSON result."""
    if body is None:
        body = dict(_FABRICATED) if route == "complete" else {}
    with tempfile.TemporaryDirectory() as tmp:
        scenario_path = Path(tmp) / "scenario.json"
        result_path = Path(tmp) / "result.json"
        scenario_path.write_text(
            json.dumps(
                {
                    "route": route,
                    "token": token,
                    "body": body,
                    "headers": headers or {},
                    # Share one store across two child runs to simulate a
                    # restart; each run is a separate interpreter.
                    "queue_dir": str(queue_dir) if queue_dir else None,
                    "skip_seed": skip_seed,
                    "seeded_id": seeded_id,
                    "stub_side_effects": stub_side_effects,
                    "fail_insert": fail_insert,
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(scenario_path), str(result_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0 or not result_path.exists():
            raise AssertionError(
                f"child failed for route={route} token={token!r}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return json.loads(result_path.read_text(encoding="utf-8"))


class TestBacklogEndpointsRefuseUnauthenticatedCallers(_BacklogAuthzCase):
    """No token configured is a 503, not an open door.

    Deliberately stricter than queueing, which is opt-in and harmless when off:
    these endpoints mutate incident state, so an unset secret must refuse.
    """

    def test_unset_secret_refuses_claim(self):
        r = _run("claim", token=None)
        self.assertEqual(503, r["status"])
        # Claim takes a lease rather than popping, so depth alone cannot
        # distinguish refused from successful. What matters is that nothing was
        # leased: an unauthorised caller must leave the clip untouched.
        self.assertIsNone(r["lease_worker_id"], "refused claim must not take a lease")
        self.assertEqual(1, r["queue_depth"])

    def test_unset_secret_refuses_complete(self):
        r = _run("complete", token=None)
        self.assertEqual(503, r["status"])
        self.assertEqual(0, r["calls_rows"], "refused complete must not write a call")
        self.assertTrue(r["seed_present"], "refused complete must not delete queued audio")

    def test_missing_token_refuses_claim(self):
        r = _run("claim", token="s" * 64, body={})
        self.assertEqual(401, r["status"])
        self.assertIsNone(r["lease_worker_id"])
        self.assertEqual(1, r["queue_depth"])

    def test_missing_token_refuses_complete(self):
        r = _run("complete", token="s" * 64, body={})
        self.assertEqual(401, r["status"])
        self.assertEqual(0, r["calls_rows"])
        self.assertTrue(r["seed_present"], "refused complete must not delete queued audio")

    def test_wrong_token_refuses_complete(self):
        r = _run("complete", token="s" * 64, body={"token": "wrong"})
        self.assertEqual(401, r["status"])
        self.assertEqual(0, r["calls_rows"], "a wrong token must not fabricate a call")
        self.assertEqual(1, r["queue_depth"], "refused complete must not discard the clip")
        # Depth alone is not enough: a refused request that wrongly removed the
        # item and then reported a clean depth would pass the check above.
        self.assertTrue(
            r["seed_present"],
            "an unauthenticated complete must not delete queued audio",
        )

    def test_non_ascii_token_is_401_not_500(self):
        """compare_digest raises TypeError on a non-ASCII str.

        Header values arrive as latin-1, so a stray byte used to turn an auth
        failure into a 500. The guard compares encoded bytes.
        """
        r = _run("complete", token="s" * 64, headers={"X-Backlog-Token": "café"})
        self.assertEqual(401, r["status"])
        self.assertNotEqual(500, r["status"])


class TestBacklogEndpointsAcceptAValidWorker(_BacklogAuthzCase):
    def test_valid_token_reaches_claim(self):
        r = _run("claim", token="s" * 64, body={"token": "s" * 64})
        self.assertEqual(200, r["status"])
        self.assertEqual("ok", (r["body"] or {}).get("status"))
        item = (r["body"] or {}).get("item") or {}
        self.assertEqual(12345, item.get("tgid"))
        self.assertEqual("seeding", item.get("tag"))
        # The clip is leased, not removed: if the worker dies the lease expires
        # and the audio comes back instead of being lost.
        self.assertIsNotNone(r["lease_worker_id"], "an authorised claim must take a lease")
        self.assertEqual(1, r["queue_depth"])

    def test_empty_transcript_discards_the_clip(self):
        """A poison item must not block the queue forever.

        The old in-memory queue popped on claim, so an empty transcript consumed
        the item. Under a lease it would expire, be re-claimed, and loop --
        starving every clip behind it. `complete` with an empty transcript must
        therefore remove the item.
        """
        r = _run(
            "complete",
            token="s" * 64,
            body={"token": "s" * 64, "item_id": "SEEDED_ID", "transcript": ""},
        )
        self.assertEqual(200, r["status"])
        self.assertEqual(0, r["queue_depth"], "empty transcript must clear the item")
        self.assertFalse(r["seed_present"], "the clip and its metadata must both be gone")

    def test_valid_token_via_bearer_reaches_complete(self):
        # Empty transcript: the handler returns early as "empty", so the test
        # proves the credential was accepted without spending an LLM call.
        r = _run(
            "complete",
            token="s" * 64,
            body={"item_id": "x", "transcript": ""},
            headers={"Authorization": "Bearer " + "s" * 64},
        )
        self.assertEqual(200, r["status"])


class TestBacklogCompletionLifecycle(_BacklogAuthzCase):
    """A completed clip must leave the queue, and only after it is stored.

    Mutation testing showed the success path was unpinned: deleting the
    `remove_queued_audio` call after a successful `complete` passed every test,
    because nothing drove a real processed result. That bug re-claims and
    re-transcribes the same audio forever.
    """

    TOKEN = "s" * 64

    def test_failed_complete_keeps_the_clip_for_retry(self):
        """Ordering matters: remove only after the transcript is stored.

        If the clip is unlinked first, a transient failure -- a locked database,
        a Talk outage -- silently destroys audio that upstream recorders already
        discarded on our 202. That is the C5 data-loss problem reproduced inside
        the fix for it.
        """
        r = _run(
            "complete",
            token=self.TOKEN,
            body={
                "token": self.TOKEN,
                "item_id": "SEEDED_ID",
                "transcript": "Engine 12 responding to a structure fire.",
                "tgid": 12345,
                "tag": "seeding",
                "duration": 4.0,
            },
            stub_side_effects=True,
            fail_insert=True,
        )
        self.assertEqual(500, r["status"])
        self.assertEqual(0, r["calls_rows"])
        self.assertTrue(
            r["seed_present"],
            "a failed complete must retain the clip; its lease will expire and "
            "it becomes claimable again",
        )

    def test_successful_complete_stores_the_call_and_clears_the_clip(self):
        r = _run(
            "complete",
            token=self.TOKEN,
            body={
                "token": self.TOKEN,
                "item_id": "SEEDED_ID",
                "transcript": "Engine 12 responding to a structure fire on Barton Springs.",
                "tgid": 12345,
                "tag": "seeding",
                "duration": 4.0,
            },
            stub_side_effects=True,
        )
        self.assertEqual(200, r["status"])
        self.assertEqual("processed", (r["body"] or {}).get("status"))
        self.assertEqual(1, r["calls_rows"], "the transcript must be durably stored")
        self.assertFalse(
            r["seed_present"],
            "a completed clip must be removed, or it is re-claimed and "
            "re-transcribed forever",
        )
        self.assertEqual(0, r["queue_depth"])


class TestBacklogDerivesCategoryFromTag(_BacklogAuthzCase):
    """Backlogged calls must be categorised like live ones.

    #163 established that the server-side TSV is a bad authority for category:
    it filed TCSO ADAM-WEST as TCEMS and left Bee Cave, AFD and every TCSO
    channel on "Unknown", so /receive derives the category from the recorder's
    tag instead.

    The backlog path kept reading the TSV. Found end to end against a real
    worker: a backlogged call came back category "Unknown" while the identical
    tag arriving on /receive was categorised correctly. Only calls that happened
    to be backlogged were affected, which is a poor class of bug to meet later.
    """

    TOKEN = "s" * 64
    TCSO_TAG = "TCSO BAKER-EAST"

    def test_backlogged_call_is_categorised_from_its_tag(self):
        r = _run(
            "complete",
            token=self.TOKEN,
            body={
                "token": self.TOKEN,
                "item_id": "SEEDED_ID",
                "transcript": "Dispatch, we are handling a traffic stop on Barton Springs.",
                "tgid": 12345,
                "tag": self.TCSO_TAG,
                "duration": 4.0,
            },
            stub_side_effects=True,
        )
        self.assertEqual(200, r["status"])
        self.assertEqual(1, r["calls_rows"])
        stored_category = r["last_call_category"]
        self.assertNotEqual(
            "Unknown", stored_category,
            "the tag is authoritative for category; deriving it from the TSV is "
            "the exact defect #163 fixed on the live path",
        )
        # The incident engine and the Talk post read the call dict, not the
        # database row. A fix that only corrects insert_call leaves an incident
        # filed under the wrong category, which is the part that reaches a map.
        self.assertEqual(
            stored_category, r["incident_category"],
            "the category handed to analyze_for_incident must match the stored one",
        )

    def test_enqueue_time_coordinates_survive_the_round_trip(self):
        """/receive resolves the right default at enqueue time.

        complete re-derived coordinates from the TSV, which has no entry for many
        real talkgroups -- landing those calls at lat/lon 0, i.e. Null Island.
        """
        r = _run(
            "complete",
            token=self.TOKEN,
            body={
                "token": self.TOKEN,
                "item_id": "SEEDED_ID",
                "transcript": "Dispatch, traffic stop on Barton Springs.",
                "tgid": 12345,
                "tag": self.TCSO_TAG,
                "duration": 4.0,
            },
            stub_side_effects=True,
        )
        self.assertEqual(200, r["status"])
        self.assertAlmostEqual(30.2672, r["last_call_lat"], places=4)
        self.assertAlmostEqual(-97.7431, r["last_call_lon"], places=4)
        self.assertAlmostEqual(
            30.2672, r["incident_lat"], places=4,
            msg="the incident must be placed at the enqueue-time coordinates",
        )

    def test_uncategorisable_tag_falls_back_rather_than_guessing(self):
        r = _run(
            "complete",
            token=self.TOKEN,
            body={
                "token": self.TOKEN,
                "item_id": "SEEDED_ID",
                "transcript": "Some traffic on an unknown channel.",
                "tgid": 12345,
                "tag": "ZZZ-NOT-A-REAL-CHANNEL",
                "duration": 4.0,
            },
            stub_side_effects=True,
        )
        self.assertEqual(200, r["status"])
        self.assertEqual(1, r["calls_rows"])
        # A tag matching no pattern must still store honestly, not invent one.
        self.assertEqual("Unknown", r["last_call_category"])


class TestBacklogSurvivesRestart(_BacklogAuthzCase):
    """The whole point of the change: a queued clip outlives the process.

    Before this, the remote-worker queue was an in-memory deque, so every clip
    waiting for a worker was lost the moment the service restarted -- which is
    exactly when a backlog is most likely to be under pressure.

    Two child runs against one shared queue directory is a restart: separate
    interpreters, no shared memory, so a clip that is still claimable in the
    second one demonstrably came off disk. Asserts the audio bytes survive too,
    not just the metadata, and that a discarded clip does not reappear.
    """

    TOKEN = "s" * 64

    def test_queued_clip_is_claimable_after_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"

            first = _run("claim", token=None, queue_dir=queue_dir)
            # The seeded clip exists in a fresh interpreter...
            self.assertEqual(503, first["status"])
            self.assertEqual(1, first["queue_depth"])

            # ...and a brand new interpreter can still claim it.
            second = _run(
                "claim", token=self.TOKEN, body={"token": self.TOKEN},
                queue_dir=queue_dir, skip_seed=True,
                seeded_id=first["seeded_id"],
            )
            self.assertEqual(200, second["status"])
            self.assertEqual("ok", (second["body"] or {}).get("status"))
            item = (second["body"] or {}).get("item") or {}
            self.assertEqual(first["seeded_id"], item.get("id"))
            self.assertEqual("seeding", item.get("tag"))
            self.assertTrue(item.get("audio_b64"))
            import base64 as _b64
            self.assertEqual(b"HELLO", _b64.b64decode(item["audio_b64"]))

    def test_completed_clip_is_gone_after_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"
            first = _run("claim", token=None, queue_dir=queue_dir)
            self.assertEqual(1, first["queue_depth"])

            _run(
                "complete", token=self.TOKEN,
                body={"token": self.TOKEN, "item_id": first["seeded_id"],
                      "transcript": ""},
                queue_dir=queue_dir, skip_seed=True,
            )
            after = _run(
                "claim", token=self.TOKEN, body={"token": self.TOKEN},
                queue_dir=queue_dir, skip_seed=True,
            )
            self.assertEqual(200, after["status"])
            self.assertEqual(
                "no_work", (after["body"] or {}).get("status"),
                "a discarded clip must not reappear after a restart",
            )


class TestBacklogTokenIsNeverEchoed(_BacklogAuthzCase):
    """The guard must return None on success, not the parsed request body.

    Returning `data` made Flask jsonify the request body, so a worker that
    supplied the token in the body received it straight back in the response.
    """

    SECRET = "c" * 64

    def test_token_in_body_is_not_reflected(self):
        r = _run("claim", token=self.SECRET, body={"token": self.SECRET})
        self.assertEqual(200, r["status"])
        self.assertNotIn(self.SECRET, r["raw_body"])

    def test_token_in_body_is_not_reflected_on_complete(self):
        r = _run(
            "complete",
            token=self.SECRET,
            body={"token": self.SECRET, "item_id": "x", "transcript": ""},
        )
        self.assertEqual(200, r["status"])
        self.assertNotIn(self.SECRET, r["raw_body"])


if __name__ == "__main__":
    unittest.main()