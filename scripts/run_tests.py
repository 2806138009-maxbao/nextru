"""Reproducible test runner: unittest discovery + machine-readable results.

Usage:
    python scripts/run_tests.py
Exit code is non-zero if any test fails/errors.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "artifacts" / "test-results.json"


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[dict] = []
        self._started: dict[str, float] = {}

    def startTest(self, test):  # noqa: N802
        self._started[test.id()] = time.perf_counter()
        super().startTest(test)

    def _push(self, test, status: str, detail: str = "") -> None:
        started = self._started.get(test.id(), time.perf_counter())
        self.records.append({
            "test": test.id(),
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "detail": detail,
        })

    def addSuccess(self, test):  # noqa: N802
        super().addSuccess(test)
        self._push(test, "PASS")

    def addFailure(self, test, err):  # noqa: N802
        super().addFailure(test, err)
        self._push(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):  # noqa: N802
        super().addError(test, err)
        self._push(test, "ERROR", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):  # noqa: N802
        super().addSkip(test, reason)
        self._push(test, "SKIP", reason)


def main() -> int:
    cases = ROOT / "reports" / "artifacts" / "cases.jsonl"
    if cases.exists():
        cases.unlink()  # keep the evidence log for THIS run only
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"),
                                                pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult)
    result = runner.run(suite)
    records = getattr(result, "records", [])
    summary = {
        "total": result.testsRun,
        "passed": sum(1 for r in records if r["status"] == "PASS"),
        "failed": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "database": "sqlite3 (isolated per test)",
        "tests": records,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps({k: summary[k] for k in
                      ("total", "passed", "failed", "errors", "skipped")},
                     ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
