"""T9 — Policy × Current × Event: inherited principles, duplicate events, new
official requirements, and an authorized user decision change.
Contract refs: §04 INV-01, §05 dynamic project resolution, T9."""

from __future__ import annotations

import unittest

from helpers import NexusTest, summarize


class T9Events(NexusTest):
    def _decision(self):
        return self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01"
        )

    def test_t9_duplicate_old_event_does_not_trigger_redecision(self):
        before = self._decision()
        first = self.core.evaluate_event("ev-dup-email", caller="system:test-harness", scope="project:DLC-01")
        second = self.core.evaluate_event("ev-dup-email", caller="system:test-harness", scope="project:DLC-01")
        after = self._decision()
        self.assertStatus(first, "OK", "T9 duplicate")
        self.assertFalse(first["result"]["outcome"]["is_new_material_delta"])
        self.assertEqual(first["result"]["outcome"]["action"], "NONE")
        self.assertFalse(first["result"]["outcome"]["decision_changed"])
        self.assertTrue(second["result"]["idempotent"])
        self.assertEqual(before["state"]["value"]["decision_id"],
                         after["state"]["value"]["decision_id"])
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_id = 'ev-dup-email'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.record(
            "T9-P1", "旧重复事件不触发重新决策（且幂等）",
            {"event": "ev-dup-email", "replay": True},
            {"is_new_material_delta": False, "action": "NONE",
             "decision_changed": False, "idempotent_replay": True},
            {"first": first["result"]["outcome"],
             "second_idempotent": second["result"]["idempotent"]},
        )

    def test_t9_new_official_requirement_is_bounded_and_does_not_overwrite(self):
        before = self._decision()
        env = self.core.evaluate_event("ev-official-new", caller="system:test-harness", scope="project:DLC-01")
        self.assertStatus(env, "OK", "T9 official")
        outcome = env["result"]["outcome"]
        self.assertTrue(outcome["is_new_material_delta"])
        self.assertTrue(outcome["requires_user_decision"])
        self.assertFalse(outcome["decision_changed"])
        self.assertEqual(outcome["bounded_reevaluation"]["max_depth"], 1)
        self.assertEqual(set(outcome["impact_scope"]), {"decision", "status"})
        after = self._decision()
        self.assertEqual(before["state"]["value"]["decision_id"],
                         after["state"]["value"]["decision_id"])
        self.assertEqual(after["state"]["value"]["decision_id"], "D1")
        self.record(
            "T9-P2", "新正式要求只触发有界重新评估，不自动改写用户决定",
            {"event": "ev-official-new"},
            {"decision_changed": False, "requires_user_decision": True,
             "impact_scope": ["decision", "status"], "max_depth": 1},
            {"outcome": outcome, "decision": after["state"]["value"]["decision_id"]},
        )

    def test_t9_user_decision_changes_current_with_authority(self):
        env = self.core.evaluate_event("ev-user-decision", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(env, "OK", "T9 user decision")
        outcome = env["result"]["outcome"]
        self.assertTrue(outcome["decision_changed"])
        self.assertEqual(outcome["previous_revision"], 1)
        self.assertEqual(outcome["new_revision"], 2)
        after = self._decision()
        self.assertEqual(after["state"]["resolution"], "CURRENT")
        self.assertEqual(after["state"]["value"]["decision_id"], "D2")
        self.assertEqual(after["state"]["value"]["supersedes"], "D1")
        self.assertEqual(after["state"]["revision"], 2)
        self.assertIn("模块 A/B", after["state"]["value"]["statement"])
        history = self.store.get_aspect_history("project/DLC-01", "decision")
        self.assertEqual(len(history), 2)
        self.assertEqual([h["is_current"] for h in history], [0, 1])
        self.record(
            "T9-P3", "用户新决定依适用权限改变 Current，并保留旧版本",
            {"event": "ev-user-decision", "expected_revision": 1},
            {"decision": "D2", "revision": 2, "supersedes": "D1",
             "history_rows": 2},
            {"outcome": outcome, "state": summarize(after)},
        )

    def test_t9_negative_unauthorized_author_cannot_change_decision(self):
        env = self.core.evaluate_event("ev-wrong-owner", caller="system:test-harness", scope="project:DLC-01")
        self.assertStatus(env, "AUTHORITY_DENIED", "T9 wrong owner")
        after = self._decision()
        self.assertEqual(after["state"]["value"]["decision_id"], "D1")
        self.assertEqual(after["state"]["revision"], 1)
        self.record(
            "T9-N1", "越权事件（外部来源冒充用户决定）被拒绝",
            {"event": "ev-wrong-owner", "authority_owner": "external:exu"},
            {"status": "AUTHORITY_DENIED", "decision": "D1", "revision": 1},
            {"error": env["error"], "state": summarize(after)},
        )

    def test_t9_negative_version_conflict_blocks_stale_write(self):
        env = self.core.evaluate_event("ev-user-stale", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(env, "VERSION_CONFLICT", "T9 version")
        after = self._decision()
        self.assertEqual(after["state"]["value"]["decision_id"], "D1")
        self.assertEqual(after["state"]["revision"], 1)
        self.record(
            "T9-N2", "预期 Revision 不符时拒绝写入（VERSION_CONFLICT）",
            {"event": "ev-user-stale", "expected_revision": 5, "actual": 1},
            {"status": "VERSION_CONFLICT", "decision_unchanged": True},
            {"error": env["error"], "decision": after["state"]["value"]["decision_id"]},
        )

    def test_t9_intent_routing_start_class_vs_learning_progress(self):
        start = self.core.get_context(
            intent="start_class", object_id="project/DLC-01",
            scope="project:DLC-01", max_chars=8000,
        )
        progress = self.core.get_context(
            intent="learning_progress", object_id="project/DLC-01",
            scope="project:DLC-01", max_chars=8000,
        )
        self.assertStatus(start, "OK", "T9 start_class")
        self.assertStatus(progress, "OK", "T9 learning_progress")
        self.assertIn("policy", start["result"]["package"])
        self.assertEqual(start["result"]["package"]["policy"]["display_name"],
                         "最小闭环优先原则")
        self.assertIn("policy:constraint", start["execution"]["retrieval_path"])
        self.assertNotIn("policy", progress["result"]["package"])
        self.assertNotIn("policy:constraint", progress["execution"]["retrieval_path"])
        self.assertEqual(list(progress["result"]["package"]["aspects"].keys()),
                         ["learner_state"])
        self.assertIn("learner_state", progress["result"]["package"]["aspects"])
        self.record(
            "T9-P4", "“开始上课”与“学到哪里”激活不同知识路径",
            {"intents": ["start_class", "learning_progress"]},
            {"start_class": ["policy", "decision", "events"],
             "learning_progress": ["learner_state"],
             "learning_progress_has_policy": False},
            {"start_path": start["execution"]["retrieval_path"],
             "progress_path": progress["execution"]["retrieval_path"],
             "progress_aspects": list(progress["result"]["package"]["aspects"].keys())},
        )

    def test_t9_temporal_as_of_query(self):
        self.core.evaluate_event("ev-user-decision", caller="user:owner", scope="project:DLC-01")
        before = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01",
            as_of="2026-10-27T00:00:00Z",
        )
        after = self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope="project:DLC-01",
            as_of="2026-10-29T00:00:00Z",
        )
        self.assertEqual(before["state"]["resolution"], "CURRENT")
        self.assertEqual(before["state"]["value"]["decision_id"], "D1")
        self.assertEqual(after["state"]["value"]["decision_id"], "D2")
        self.assertStatus(after, "OK", "T9 as_of")
        self.record(
            "T9-P6", "as_of 时间点查询返回当时有效的 Current",
            {"as_of": ["2026-10-27", "2026-10-29"]},
            {"2026-10-27": "D1", "2026-10-29": "D2"},
            {"before": before["state"]["value"]["decision_id"],
             "after": after["state"]["value"]["decision_id"]},
        )

    def test_t9_projection_refresh_version_guard(self):
        stale = self.core.refresh_projection(
            "SIM_DRIVE", "sim-dlc-mission", "r2", projection_id="proj-dlc-mission", caller="system:test-harness"
        )
        self.assertStatus(stale, "VERSION_CONFLICT", "T9 refresh stale")
        fresh = self.core.refresh_projection(
            "SIM_DRIVE", "sim-dlc-mission", "r1", projection_id="proj-dlc-mission", caller="system:test-harness"
        )
        self.assertStatus(fresh, "OK", "T9 refresh ok")
        self.record(
            "T9-P5", "投影重建以预期来源 Revision 守卫",
            {"expected": "r2", "head": "r1"},
            {"stale": "VERSION_CONFLICT", "refresh_at_r1": "OK"},
            {"stale": stale["execution"]["status"],
             "fresh": fresh["execution"]["status"]},
        )


if __name__ == "__main__":
    unittest.main()
