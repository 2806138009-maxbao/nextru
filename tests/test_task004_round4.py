"""Final scope-boundary regression: global must not read scoped evidence."""

from __future__ import annotations

import json
import unittest

from helpers import NexusTest


class Task004Round4(NexusTest):
    PROTECTED = ["商单", "PROTECTED", "CROSS-SCOPE", "2023-12"]

    def _assert_no_content(self, env, label: str) -> None:
        self.assertEqual(env["execution"]["status"], "INVALID_SCOPE", label)
        self.assertIsNone(env["result"], label)
        blob = json.dumps(env, ensure_ascii=False)
        for marker in self.PROTECTED:
            self.assertNotIn(marker, blob, label)

    def test_global_cannot_read_scoped_observation_or_source_ref(self):
        obs = self.core.trace_evidence(
            "obs-sponsor-1", scope="global", expand=True)
        date = self.core.trace_evidence(
            "src/video-a-001", scope="global", expand=True)
        self._assert_no_content(obs, "global observation")
        self._assert_no_content(date, "global source ref")

        explicit_obs = self.core.trace_evidence(
            "obs-sponsor-1", scope="creator:CREATOR-A", expand=True)
        explicit_date = self.core.trace_evidence(
            "src/video-a-001", scope="creator:CREATOR-A", expand=True)
        self.assertStatus(explicit_obs, "OK", "explicit observation")
        self.assertStatus(explicit_date, "OK", "explicit source ref")
        self.assertIn("statement", explicit_obs["result"]["item"]["observation"])

    def test_global_cannot_read_scoped_aspect_version(self):
        env = self.core.trace_evidence(
            "project/DLC-01:decision:r1", scope="global")
        self._assert_no_content(env, "global aspect version")
        explicit = self.core.trace_evidence(
            "project/DLC-01:decision:r1", scope="project:DLC-01")
        self.assertStatus(explicit, "OK", "explicit aspect version")

    def test_global_cannot_read_scoped_relation(self):
        env = self.core.trace_evidence(
            "rel-governed", scope="global", expand=True)
        self._assert_no_content(env, "global relation")
        explicit = self.core.trace_evidence(
            "rel-governed", scope="project:DLC-01", expand=True)
        self.assertStatus(explicit, "OK", "explicit relation")

    def test_direct_and_indirect_scope_checks_are_consistent(self):
        cases = [
            ("src/video-a-001", "creator:CREATOR-A"),
            ("obs-sponsor-1", "creator:CREATOR-A"),
            ("project/DLC-01:decision:r1", "project:DLC-01"),
            ("rel-governed", "project:DLC-01"),
        ]
        for item_id, actual_scope in cases:
            global_env = self.core.trace_evidence(item_id, scope="global")
            self._assert_no_content(global_env, f"global {item_id}")
            explicit = self.core.trace_evidence(item_id, scope=actual_scope)
            self.assertStatus(explicit, "OK", f"explicit {item_id}")
        self.record(
            "T4R4-G1", "global 不得通过任何证据入口读取具体 Scope",
            {"entries": [c[0] for c in cases]},
            {"global": "INVALID_SCOPE/result=None",
             "explicit": "OK"},
            {"global": "denied", "explicit": "ok"},
        )


if __name__ == "__main__":
    unittest.main()
