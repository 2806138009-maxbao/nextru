"""Run the five acceptance-scenario example queries and save requests/responses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
from nexus.core import NexusCore
from nexus.db import Store, connect
from nexus.service import TEST_OPERATIONS, dispatch, dispatch_test

ROOT = Path(__file__).resolve().parents[1]

EXAMPLES = [
    ("T1_current_decision", "get_current_state",
     {"object_id": "project/DLC-01", "aspect": "decision",
      "scope": "project:DLC-01"}),
    ("T2_reverse_and_hops", "traverse_relations",
     {"object_id": "principle/P-01", "relation_types": ["GOVERNED_BY", "DEPENDS_ON"],
      "direction": "both", "max_depth": 2, "scope": "project:DLC-01"}),
    ("T7_first_video_status", "get_context",
     {"intent": "first_video_status", "object_id": "creative/FIRST-VIDEO",
      "scope": "creative:first-video", "max_chars": 6000}),
    ("T8_profit_boundary", "get_context",
     {"intent": "profit", "object_id": "video/CREATOR-A-V001",
      "scope": "creator:CREATOR-A"}),
    ("T9_learning_progress", "get_context",
     {"intent": "learning_progress", "object_id": "project/DLC-01",
      "scope": "project:DLC-01", "max_chars": 6000}),
    ("T9_official_event", "evaluate_event",
     {"event_id": "ev-official-new", "scope": "project:DLC-01"}),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "var" / "nexus.db"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "examples.json"))
    args = parser.parse_args()

    conn = connect(args.db)
    core = NexusCore(Store(conn))
    records = []
    for name, operation, params in EXAMPLES:
        if operation in TEST_OPERATIONS:
            env = dispatch_test(core, operation, params, "system:test-harness")
        else:
            env = dispatch(core, operation, params)
        records.append({"example": name, "request": {"operation": operation,
                                                     "params": params},
                        "response": env})
        print(f"{name:26s} {env['execution']['status']:12s} "
              f"chars={len(json.dumps(env, ensure_ascii=False))}")
    Path(args.out).write_text(json.dumps(records, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"wrote {args.out}")
    conn.close()


if __name__ == "__main__":
    main()
