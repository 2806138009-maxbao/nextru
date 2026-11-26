"""Error/failure-state coverage required by the task book:
STALE, BLOCKED, UNKNOWN, CONFLICTED, NOT_CHECKED, INVALID_SCOPE, VERSION_CONFLICT,
plus PARTIAL and NOT_FOUND."""

from __future__ import annotations

import unittest

from helpers import NexusTest, summarize


class ErrorStates(NexusTest):
    def test_blocked(self):
        self.store.conn.execute(
            "UPDATE source_refs SET status = 'BLOCKED' WHERE source_ref_id = 'src/dlc-mission-r1'"
        )
        env = self.core.get_current_state("project/DLC-01", "decision", "project:DLC-01")
        self.assertStatus(env, "BLOCKED", "BLOCKED")
        self.assertIsNone(env["state"]["value"])
        self.record("E-BLOCKED", "来源权限阻断时不返回缓存内容",
                    {"source_ref": "src/dlc-mission-r1", "status": "BLOCKED"},
                    {"status": "BLOCKED", "value": None}, summarize(env))

    def test_not_checked(self):
        self.store.conn.execute(
            "DELETE FROM source_heads WHERE native_id = 'sim-dlc-mission'"
        )
        env = self.core.get_current_state("project/DLC-01", "decision", "project:DLC-01")
        self.assertStatus(env, "NOT_CHECKED", "NOT_CHECKED")
        self.assertEqual(env["state"]["resolution"], "SNAPSHOT_ONLY")
        self.assertFalse(env["state"]["is_latest_verified"])
        self.record("E-NOT-CHECKED", "来源未检查与过期来源可区分",
                    {"source_head": "absent"},
                    {"status": "NOT_CHECKED", "is_latest_verified": False},
                    summarize(env))

    def test_stale(self):
        self.store.set_source_head("SIM_DRIVE", "sim-dlc-mission", "r9",
                                   "2026-10-29T00:00:00Z")
        env = self.core.get_current_state("project/DLC-01", "decision", "project:DLC-01")
        self.assertStatus(env, "STALE", "STALE")
        self.record("E-STALE", "来源版本前移后投影标记 STALE",
                    {"head": "r9", "projection": "r1"},
                    {"status": "STALE"}, summarize(env))

    def test_conflicted(self):
        self.store.add_conflict(
            "conflict-status", "project/DLC-01", "status",
            "两个官方来源给出不同的项目阶段",
            [{"value": "IN_PROGRESS", "source": "sim-dlc-mission"},
             {"value": "PAUSED", "source": "sim-exu-email"}],
            ["src/dlc-mission-r1", "src/exu-email-r1"],
        )
        env = self.core.get_current_state("project/DLC-01", "status", "project:DLC-01")
        self.assertStatus(env, "CONFLICTED", "CONFLICTED")
        self.assertIsNone(env["state"]["value"])
        self.assertTrue(env["result"]["aspects"][0]["conflict_candidates"])
        self.record("E-CONFLICTED", "冲突事实不强行合并为确定结论",
                    {"aspect": "status", "candidates": 2},
                    {"status": "CONFLICTED", "value": None},
                    summarize(env))

    def test_unknown_no_authority_binding(self):
        self.store.add_object("project/NO-BINDING", "Project", "无 Authority 对象",
                              "project:DLC-01")
        self.store.add_aspect(
            "project/NO-BINDING:note:r1", "project/NO-BINDING", "note",
            {"text": "candidate note"}, "1", "CURRENT", "UNVERIFIED", 1, True,
            "2026-10-01T00:00:00Z", None, "src/dlc-mission-r1",
        )
        env = self.core.get_current_state("project/NO-BINDING", "note", "project:DLC-01")
        self.assertStatus(env, "UNKNOWN", "UNKNOWN")
        self.assertIsNone(env["state"]["value"])
        self.record("E-UNKNOWN", "缺少适用 AuthorityBinding 时返回 UNKNOWN",
                    {"object": "project/NO-BINDING"},
                    {"status": "UNKNOWN", "value": None}, summarize(env))

    def test_invalid_scope(self):
        env = self.core.get_current_state("project/DLC-01", "decision", "project:NOPE")
        self.assertStatus(env, "INVALID_SCOPE", "INVALID_SCOPE")
        self.assertIn("known_scopes", env["error"]["detail"])
        self.record("E-INVALID-SCOPE", "未注册 scope 被拒绝",
                    {"scope": "project:NOPE"},
                    {"status": "INVALID_SCOPE"}, summarize(env))

    def test_version_conflict(self):
        env = self.core.refresh_projection("SIM_DRIVE", "sim-dlc-mission", "r7", caller="system:test-harness")
        self.assertStatus(env, "VERSION_CONFLICT", "VERSION_CONFLICT")
        self.assertEqual(env["error"]["detail"]["observed_head"], "r1")
        self.record("E-VERSION-CONFLICT", "预期 Revision 与来源头不一致时拒绝重建",
                    {"expected": "r7", "head": "r1"},
                    {"status": "VERSION_CONFLICT"}, summarize(env))

    def test_partial_context_budget(self):
        env = self.core.get_context(
            "first_video_status", "creative/FIRST-VIDEO", scope="creative:first-video",
            max_chars=200,
        )
        self.assertStatus(env, "PARTIAL", "PARTIAL")
        self.assertTrue(any("CONTEXT_BUDGET_EXCEEDED" in w for w in env["warnings"]))
        self.record("E-PARTIAL", "Context 预算不足时显式 PARTIAL",
                    {"max_chars": 200},
                    {"status": "PARTIAL"}, summarize(env))


if __name__ == "__main__":
    unittest.main()
