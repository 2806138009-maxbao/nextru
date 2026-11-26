"""T1 — Current / Authority: accepted decision vs historical discussion vs
official external evidence. Contract refs: INV-01..INV-04, §05, §07."""

from __future__ import annotations

import unittest

from helpers import NexusTest, summarize


class T1CurrentAuthority(NexusTest):
    def test_t1_positive_current_decision_owner_scope_source(self):
        env = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01"
        )
        self.assertStatus(env, "OK", "T1 positive")
        state = env["state"]
        self.assertEqual(state["resolution"], "CURRENT")
        self.assertEqual(state["value"]["decision_id"], "D1")
        self.assertEqual(state["verification"], "VERIFIED")
        self.assertEqual(state["freshness"], "FRESH")
        self.assertTrue(state["is_latest_verified"])
        self.assertEqual(env["authority"]["owner"], "user:owner")
        self.assertEqual(env["authority"]["scope"], "project:DLC-01")
        self.assertEqual(env["authority"]["write_policy"], "USER_ONLY")
        self.assertEqual(env["evidence"][0]["native_id"], "sim-dlc-mission")
        self.assertEqual(env["evidence"][0]["revision"], "r1")
        self.assertEqual(env["evidence"][0]["anchor"], "§2 决定")
        self.record(
            "T1-P1", "精确查询返回正确 Owner/Scope/SourceRef",
            {"object_id": "project/DLC-01", "aspect": "decision",
             "scope": "project:DLC-01"},
            {"resolution": "CURRENT", "decision_id": "D1", "owner": "user:owner",
             "scope": "project:DLC-01", "native_id": "sim-dlc-mission"},
            summarize(env),
        )

    def test_t1_negative_historical_is_not_current(self):
        env = self.core.get_current_state(
            "discussion/DLC-01-LEGACY-2026-09", aspect="decision",
            scope="project:DLC-01",
        )
        self.assertStatus(env, "OK", "T1 historical")
        self.assertEqual(env["state"]["resolution"], "HISTORICAL_ONLY")
        self.assertIsNone(env["state"]["value"], "historical value must not be served as Current")
        self.assertIsNone(env["state"]["lifecycle"] if False else env["state"].get("value"))
        self.assertIn("HISTORICAL_NOT_CURRENT", " ".join(env["warnings"]))
        aspect = env["result"]["aspects"][0]
        self.assertEqual(aspect["historical_value"]["status"], "NOT_DECISION")
        self.record(
            "T1-N1", "旧历史讨论不得冒充 Current",
            {"object_id": "discussion/DLC-01-LEGACY-2026-09", "aspect": "decision"},
            {"resolution": "HISTORICAL_ONLY", "value": None},
            summarize(env),
        )

    def test_t1_negative_official_fact_does_not_change_user_decision(self):
        before = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01"
        )
        ev = self.core.evaluate_event("ev-official-new", caller="system:test-harness", scope="project:DLC-01")
        self.assertStatus(ev, "OK", "T1 official event")
        outcome = ev["result"]["outcome"]
        self.assertTrue(outcome["is_new_material_delta"])
        self.assertFalse(outcome["decision_changed"])
        self.assertTrue(outcome["requires_user_decision"])
        after = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01"
        )
        self.assertEqual(
            before["state"]["value"]["decision_id"],
            after["state"]["value"]["decision_id"],
        )
        official = self.core.get_current_state(
            "external/EXU-LAB-2026-10-27", aspect="requirement", scope="external:exu"
        )
        self.assertEqual(official["state"]["resolution"], "CURRENT")
        self.assertEqual(official["authority"]["owner"], "external:exu")
        self.record(
            "T1-N2", "官方邮件事实不自动升级为用户决定",
            {"event": "ev-official-new"},
            {"decision_changed": False, "requires_user_decision": True,
             "decision_before_after": "D1"},
            {"outcome": outcome, "after": summarize(after),
             "official_owner": official["authority"]["owner"]},
        )

    def test_t1_negative_unknown_scope(self):
        env = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:OTHER"
        )
        self.assertStatus(env, "INVALID_SCOPE", "T1 invalid scope")
        self.record(
            "T1-N3", "未注册或越界 scope 被拒绝",
            {"scope": "project:OTHER"},
            {"status": "INVALID_SCOPE"},
            summarize(env),
        )

    def test_t1_negative_not_found_is_not_absence(self):
        env = self.core.get_current_state("project/DOES-NOT-EXIST", scope="global")
        self.assertStatus(env, "NOT_FOUND", "T1 not found")
        self.assertFalse(env["error"]["detail"]["absence_is_conclusive"])
        self.record(
            "T1-N4", "未找到不等于不存在",
            {"object_id": "project/DOES-NOT-EXIST"},
            {"status": "NOT_FOUND", "absence_is_conclusive": False},
            {"status": env["execution"]["status"],
             "absence_is_conclusive": env["error"]["detail"]["absence_is_conclusive"]},
        )

    def test_t1_q2_snapshot_is_never_claimed_as_latest(self):
        # Simulate the source moving ahead of the stored projection.
        self.store.set_source_head("SIM_DRIVE", "sim-dlc-mission", "r2",
                                   "2026-10-29T00:00:00Z")
        strict = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01",
            mode="strict_latest",
        )
        self.assertStatus(strict, "STALE", "T1 Q2 strict")
        self.assertEqual(strict["state"]["resolution"], "SNAPSHOT_ONLY")
        self.assertFalse(strict["state"]["is_latest_verified"])
        self.assertTrue(any("Q2_SPEC_OPEN" in w for w in strict["warnings"]))

        snapshot = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01",
            mode="snapshot_ok",
        )
        self.assertStatus(snapshot, "OK", "T1 Q2 snapshot")
        self.assertFalse(snapshot["state"]["is_latest_verified"])
        self.assertEqual(
            snapshot["result"]["aspects"][0]["q2_branch"], "SPEC_OPEN_Q2"
        )
        self.record(
            "T1-Q2", "Q2 OPEN：旧快照可标注返回，但不得声称最新 Current",
            {"source_head": "r2", "projection": "r1"},
            {"strict_latest": "STALE", "snapshot_ok": "SNAPSHOT_ONLY",
             "q2_branch": "SPEC_OPEN_Q2", "is_latest_verified": False},
            {"strict": summarize(strict), "snapshot": summarize(snapshot)},
            notes="Decision branch left explicitly SPEC_OPEN; no contract change made.",
        )


if __name__ == "__main__":
    unittest.main()
