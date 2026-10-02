"""Scenario runner for tests/test_durable_counters.py. Not a test file.

Each invocation is a separate interpreter against a shared database file, which
is what makes "survives a restart" testable without actually restarting anything:
there is no in-process state carried across the calls, only the SQLite file.

DB_PATH is set before the first project import, because modules.database does
`from modules.config import DB_PATH` and freezes the path at import time.

Usage: python _durable_counters_child.py <scenario.json> <result.json>
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
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    db_path = scenario["db_path"]

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DB_PATH"] = db_path
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
        os.environ["BB_RAW_AUDIO_QUEUE_DIR"] = str(Path(tmp) / "raw_audio_queue")
        os.environ["TIPS_UPLOAD_DIR"] = str(Path(tmp) / "tips")
        os.environ["TGID_TSV"] = str(Path(tmp) / "none.tsv")
        os.environ["HOMICIDE_SEED_PATH"] = str(Path(tmp) / "none.json")

        from modules.database import (
            bump_counter,
            init_db,
            insert_call,
            read_counter,
            read_counters,
        )

        init_db()
        result: dict = {}

        action = scenario.get("do")
        if action == "bump":
            bump_counter(scenario["name"], scenario.get("labels", ""))
            result["value"] = read_counter(scenario["name"], scenario.get("labels", ""))
        elif action == "read":
            result["value"] = read_counter(scenario["name"], scenario.get("labels", ""))
        elif action == "ingest":
            # Mirrors what _record_ingest_outcome does in audio_receiver.
            bump_counter(
                "ingest_outcome",
                f"reason={scenario['reason']},node={scenario['node']}",
            )
            result["value"] = 1.0
        elif action == "read_ingest":
            parsed = {}
            for labels, value in read_counters("ingest_outcome").items():
                bits = dict(p.split("=", 1) for p in labels.split(",") if "=" in p)
                parsed[f"{bits.get('reason')}|{bits.get('node')}"] = value
            result["ingest"] = parsed
        elif action == "insert":
            insert_call(
                1.0, 12345, scenario["tag"], "Test", "pi5", 4.0, "transcript",
                30.2672, -97.7431, None, worker=scenario.get("worker"),
            )
            result["ok"] = True
        elif action == "read_calls":
            with sqlite3.connect(db_path) as conn:
                result["calls"] = [
                    {"tag": t, "worker": w, "node": n}
                    for t, w, n in conn.execute(
                        "SELECT tag, worker, node FROM calls ORDER BY id"
                    )
                ]
        else:
            raise SystemExit(f"unknown scenario action: {action!r}")

        Path(result_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()