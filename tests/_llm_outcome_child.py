"""Scenario runner for tests/test_llm_outcome_counter.py. Not a test file.

Drives `llm_analyze` down one specific path and reports which outcome counters
that path touched. Each scenario runs in its own interpreter so module-level
state in `modules.llm` (the routine tracker, the rate-limit list, the backoff
deadline) cannot leak between scenarios -- several of these paths are only
reachable because that state was primed.

`modules.llm` does not import faster_whisper, so unlike most suites here this one
runs in ordinary development environments rather than only on the prod venv.

Usage: python _llm_outcome_child.py <scenario.json> <result.json>
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> None:
    scenario_path, result_path = sys.argv[1], sys.argv[2]
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    name = scenario["outcome"]

    # A caller may supply db_path so two runs share one database -- that is what
    # makes "survives a restart" testable at all, since each run is a separate
    # interpreter with no shared memory.
    _shared_db = scenario.get("db_path")

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DB_PATH"] = _shared_db or str(Path(tmp) / "calls.db")
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
        os.environ["TIPS_UPLOAD_DIR"] = str(Path(tmp) / "tips")
        os.environ["TGID_TSV"] = str(Path(tmp) / "none.tsv")
        os.environ["HOMICIDE_SEED_PATH"] = str(Path(tmp) / "none.json")
        # The real provider key must not be present, or a scenario intended to
        # test a path would make a billed network call.
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)

        import modules.llm as llm
        from modules.database import init_db, read_counters

        init_db()

        # --- prime the module state each scenario depends on ---------------
        llm.OPENROUTER_ENABLED = True
        llm._llm_backoff_until = 0.0
        llm._llm_call_times.clear()
        llm._llm_routine_tracker.clear()
        llm._llm_min_transcript_probe = None

        call = {
            "tgid": 1315,
            "tag": "APD-DISPATCH",
            "category": "Police Dispatch",
            # Must clear _LLM_MIN_TRANSCRIPT (50) and match _LLM_SAFETY_RE, or
            # the default call exits at skipped_short and every scenario aliases
            # onto the same path.
            "transcript": scenario.get(
                "transcript",
                "officer involved in a shooting, multiple units responding, "
                "suspect still on scene, requesting a second dispatcher",
            ),
            "duration": scenario.get("duration", 12.0),
            "location": None,
        }
        if scenario.get("tgid_zero"):
            call["tgid"] = 0

        # --- path-specific priming ----------------------------------------
        if name == "disabled":
            llm.OPENROUTER_ENABLED = False

        if name == "skipped_nonspeech":
            llm._looks_like_nonspeech = lambda _t: True

        if name == "skipped_short_duration":
            llm._LLM_MIN_DURATION = 999.0

        if name == "skipped_short":
            call["transcript"] = "uh"

        if name == "skipped_cooldown":
            # Only reachable with NO safety keyword -- the cooldown check is
            # nested inside the not-matching branch. Verify the premise here too,
            # so a future change to the regex surfaces as a failing scenario
            # rather than as a silently-unreachable path.
            llm._LLM_SAFETY_RE = __import__("re").compile(r"(?!)")
            llm._llm_routine_tracker[1315] = {
                "streak": 5, "cooldown_until": time.time() + 3600, "last_ts": time.time(),
            }

        if name == "skipped_backoff":
            llm._llm_backoff_until = time.time() + 3600

        if name == "analyzed":
            llm._call_openrouter_llm = lambda *_a, **_kw: {
                "incident_type": "Fire Dispatch", "priority": "high",
                "should_hold": False, "description": "test",
                "escalation_stage": "routine", "reasoning": "test",
            }

        if name == "error":
            def _boom(*_a, **_kw):
                raise RuntimeError("provider exploded")
            llm._call_openrouter_llm = _boom

        returned = llm.llm_analyze(call, [])

        counts = {}
        for labels, value in read_counters("llm_outcome").items():
            for part in labels.split(","):
                if part.startswith("outcome="):
                    counts[part.split("=", 1)[1]] = value

        Path(result_path).write_text(
            json.dumps({"outcomes": counts, "returned_none": returned is None}),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()