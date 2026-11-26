"""Shared test helpers: isolated SQLite DB per test + evidence recorder."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nexus.core import NexusCore  # noqa: E402
from nexus.db import Store, connect  # noqa: E402
from nexus.fixtures import reset_and_build  # noqa: E402

TEST_DB_ROOT = ROOT / "var" / "test-dbs"
TEST_DB_ROOT.mkdir(parents=True, exist_ok=True)

ARTIFACT_DIR = pathlib.Path(
    os.environ.get("NEXUS_ARTIFACTS", str(ROOT / "reports" / "artifacts"))
)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
CASES_PATH = ARTIFACT_DIR / "cases.jsonl"


def tempfile_mkdtemp() -> str:
    # NOTE: tempfile.mkdtemp() creates a 0700 ACL directory that the Windows
    # file sandbox cannot always re-open; use a normal, uniquely named dir.
    import uuid

    path = TEST_DB_ROOT / f"run-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=False)
    return str(path)


class NexusTest(unittest.TestCase):
    """Each test runs against a freshly rebuilt, isolated SQLite database."""

    def setUp(self) -> None:
        self._tmp_name = tempfile_mkdtemp()
        self.db_path = os.path.join(self._tmp_name, "nexus.db")
        self.stats = reset_and_build(self.db_path)
        self.conn = connect(self.db_path)
        self.store = Store(self.conn)
        self.core = NexusCore(self.store)

    def tearDown(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self.conn.close()
        shutil.rmtree(self._tmp_name, ignore_errors=True)

    # ------------------------------------------------------------- assertions
    def assertStatus(self, envelope: dict, expected: str, msg: str = "") -> None:
        actual = envelope["execution"]["status"]
        self.assertEqual(
            actual, expected,
            f"{msg} expected execution.status={expected}, got {actual};"
            f" error={envelope.get('error')}",
        )

    # --------------------------------------------------------------- evidence
    def record(self, case_id: str, title: str, request, expected, actual,
               verdict: str = "PASS", notes: str = "") -> None:
        row = {
            "case_id": case_id,
            "title": title,
            "request": request,
            "expected": expected,
            "actual": actual,
            "verdict": verdict,
            "notes": notes,
            "db": "sqlite3",
        }
        with CASES_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def summarize(envelope: dict, max_value_chars: int = 400) -> dict:
    """Small, stable view of an envelope for the evidence log."""
    state = envelope.get("state") or {}
    value = state.get("value")
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > max_value_chars:
        text = text[:max_value_chars] + "..."
    return {
        "status": envelope["execution"]["status"],
        "retrieval_path": envelope["execution"]["retrieval_path"],
        "resolution": state.get("resolution"),
        "value": text,
        "warnings": envelope.get("warnings"),
        "error": (envelope.get("error") or {}).get("code"),
    }
