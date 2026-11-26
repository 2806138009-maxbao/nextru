"""T7 — Long document → structured view → minimal sufficient context.
Contract refs: R6, §03 granularity, §05 dynamic context, T7 in P0 study."""

from __future__ import annotations

import unittest

from helpers import NexusTest, summarize


class T7Context(NexusTest):
    def test_t7_q1_minimal_context_does_not_load_whole_doc(self):
        env = self.core.get_context(
            intent="first_video_status",
            object_id="creative/FIRST-VIDEO",
            scope="creative:first-video",
            max_chars=6000,
        )
        self.assertStatus(env, "OK", "T7 Q1")
        pkg = env["result"]["package"]
        self.assertEqual(list(pkg["aspects"].keys()), ["first_video"])
        value = pkg["aspects"]["first_video"]["value"]
        self.assertIn("Nexus", value["topic"])
        self.assertFalse(value["shot"])
        self.assertFalse(value["scheduled"])
        self.assertEqual(value["script_status"], "NOT_STARTED")
        self.assertIn("选题已定不等于已制作", value["decisive_exception"])
        self.assertNotIn("creative_purpose", pkg["aspects"])
        self.assertNotIn("research_candidates", pkg["aspects"])
        self.assertEqual(env["execution"]["provider_io"]["full_source_reads"], 0)
        self.assertLess(
            env["result"]["package_chars"], self.stats["full_doc_chars"],
            "minimal context must be smaller than the full document",
        )
        self.record(
            "T7-Q1", "单字段查询不装载全文，且保留“未拍摄/未排期”例外",
            {"intent": "first_video_status", "max_chars": 6000},
            {"aspects": ["first_video"], "shot": False, "scheduled": False,
             "full_source_reads": 0,
             "package_chars < full_doc_chars": True},
            {"package_chars": env["result"]["package_chars"],
             "full_doc_chars": self.stats["full_doc_chars"],
             "provider_io": env["execution"]["provider_io"],
             "value": value},
        )

    def test_t7_q2_expands_evidence_and_keeps_quality_boundary(self):
        env = self.core.get_context(
            intent="opening_quality",
            object_id="creative/FIRST-VIDEO",
            scope="creative:first-video",
            depth=2,
            max_chars=12000,
        )
        self.assertStatus(env, "OK", "T7 Q2")
        pkg = env["result"]["package"]
        style = pkg["aspects"]["style_quality"]["value"]
        self.assertIn("真实表达", style["boundary"]["must_keep"])
        self.assertIn("不制造虚假钩子", style["boundary"]["must_keep"])
        self.assertIn("真实性优先", style["decisive_exception"])
        self.assertIn("decisive_exceptions", pkg)
        research = pkg["aspects"]["research_candidates"]["value"]
        self.assertFalse(research["candidates"][0]["usable_as_evidence"])
        self.assertEqual(pkg["aspects"]["research_candidates"]["verification"], "TO_TEST")
        self.assertTrue(pkg.get("expanded_sections"), "key exception needs source expansion")
        self.assertGreater(env["execution"]["provider_io"]["source_chars_returned"], 0)
        self.record(
            "T7-Q2", "需要跨章节例外时按需展开 SourceRef，且不把候选当作实证",
            {"intent": "opening_quality", "depth": 2},
            {"boundary_kept": True, "research_candidate_usable_as_evidence": False,
             "expanded_sections": ">0"},
            {"package_chars": env["result"]["package_chars"],
             "sections_read": len(pkg.get("expanded_sections", [])),
             "provider_io": env["execution"]["provider_io"]},
        )

    def test_t7_negative_small_budget_never_drops_decisive_exception(self):
        env = self.core.get_context(
            intent="opening_quality",
            object_id="creative/FIRST-VIDEO",
            scope="creative:first-video",
            depth=2,
            max_chars=800,
        )
        self.assertStatus(env, "PARTIAL", "T7 budget")
        pkg = env["result"]["package"]
        self.assertIn("decisive_exceptions", pkg)
        joined = str(pkg["decisive_exceptions"])
        self.assertIn("真实性", joined)
        self.assertTrue(any("CONTEXT_BUDGET_EXCEEDED" in w for w in env["warnings"]))
        self.record(
            "T7-N1", "预算不足时保留决定性例外并显式 PARTIAL，不静默删减",
            {"max_chars": 800},
            {"status": "PARTIAL", "decisive_exception_present": True},
            {"package_chars": env["result"]["package_chars"],
             "warnings": env["warnings"]},
        )

    def test_t7_negative_blocked_source_is_not_served(self):
        env = self.core.get_context(
            intent="release_boundaries",
            object_id="creative/FIRST-VIDEO",
            scope="creative:first-video",
        )
        self.assertStatus(env, "BLOCKED", "T7 blocked")
        entry = env["result"]["package"]["aspects"]["release_boundaries"]
        self.assertIsNone(entry["value"])
        self.assertEqual(entry["resolution"], "BLOCKED")
        self.record(
            "T7-N2", "来源被阻断时不以缓存绕过权限",
            {"intent": "release_boundaries"},
            {"status": "BLOCKED", "value": None},
            summarize(env),
        )

    def test_t7_stale_projection_is_labelled(self):
        self.store.set_source_head("SIM_DRIVE", "sim-creative-doc", "r8",
                                   "2026-10-29T00:00:00Z")
        env = self.core.get_context(
            intent="first_video_status",
            object_id="creative/FIRST-VIDEO",
            scope="creative:first-video",
        )
        self.assertStatus(env, "PARTIAL", "T7 stale context")
        entry = env["result"]["package"]["aspects"]["first_video"]
        self.assertEqual(entry["resolution"], "SNAPSHOT_ONLY")
        self.assertFalse(entry["is_latest_verified"])
        self.assertEqual(entry["q2_branch"], "SPEC_OPEN_Q2")
        self.record(
            "T7-N3", "文档更新后衍生投影进入 STALE，不冒充最新",
            {"source_head": "r8", "projection": "r7"},
            {"resolution": "SNAPSHOT_ONLY", "is_latest_verified": False},
            summarize(env),
        )


if __name__ == "__main__":
    unittest.main()
