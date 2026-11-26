"""T8 — One source, many dimensions (Creator dossier).
Contract refs: §03 "一源多维", R7, T8. Uses a de-identified simulated fixture;
video timestamps are absent and must stay absent."""

from __future__ import annotations

import unittest

from helpers import NexusTest, summarize


class T8Dossier(NexusTest):
    def test_t8_sponsorship_keeps_identity_revenue_and_disclosure(self):
        env = self.core.get_context(
            intent="sponsorship",
            object_id="video/CREATOR-A-V001",
            scope="creator:CREATOR-A",
        )
        self.assertStatus(env, "PARTIAL", "T8 sponsorship (no timecode -> PARTIAL)")
        pkg = env["result"]["package"]
        dims = {o["dimension"] for o in pkg["observations"]}
        self.assertEqual(dims, {"commercial", "disclosure"})
        self.assertTrue({"profile", "economics"} <= set(pkg["aspects"]))
        profile = pkg["aspects"]["profile"]["value"]
        economics = pkg["aspects"]["economics"]["value"]
        self.assertEqual(profile["full_time_since"], "2023-12")
        self.assertIn("20", profile["followers_reported"])
        self.assertIn("4-6", economics["revenue_reported"])
        self.assertEqual(economics["net_profit"], "NOT_VERIFIED")
        disclosure = [o for o in pkg["observations"] if o["dimension"] == "disclosure"]
        self.assertEqual(len(disclosure), 1)
        self.assertIn("赞助", disclosure[0]["statement"])
        ids = {i for o in pkg["observations"] for i in o["source_ref_ids"]}
        ids |= {i for a in pkg["aspects"].values() for i in a["source_ref_ids"]}
        evidence_ids = {r["source_ref_id"] for r in env["evidence"]}
        self.assertTrue(ids <= evidence_ids, "all referenced SourceRefs must resolve")
        self.record(
            "T8-P1", "商单问题不得因意图路由丢失身份、收入、赞助披露",
            {"intent": "sponsorship", "object": "video/CREATOR-A-V001"},
            {"dimensions": ["commercial", "disclosure"],
             "aspects": ["profile", "economics"], "status": "PARTIAL"},
            {"dimensions": sorted(dims), "aspects": sorted(pkg["aspects"]),
             "package_chars": env["result"]["package_chars"]},
        )

    def test_t8_production_stack_is_a_different_slice_of_same_source(self):
        env = self.core.get_context(
            intent="production_stack",
            object_id="video/CREATOR-A-V001",
            scope="creator:CREATOR-A",
        )
        dims = {o["dimension"] for o in env["result"]["package"]["observations"]}
        self.assertEqual(dims, {"equipment", "ai_workflow"})
        ai = [o for o in env["result"]["package"]["observations"]
              if o["dimension"] == "ai_workflow"]
        self.assertEqual(ai[0]["stance"], "CASE")
        self.assertIn("3 倍", ai[0]["statement"])
        self.assertIn("未独立测量", ai[0]["detail"])
        self.record(
            "T8-P2", "设备/AI 工作流是同一来源的另一个维度切片",
            {"intent": "production_stack"},
            {"dimensions": ["equipment", "ai_workflow"],
             "speed_claim_stance": "CASE"},
            {"dimensions": sorted(dims), "ai_observation": ai[0]},
        )

    def test_t8_revenue_is_not_net_profit(self):
        env = self.core.get_context(
            intent="profit",
            object_id="video/CREATOR-A-V001",
            scope="creator:CREATOR-A",
        )
        pkg = env["result"]["package"]
        economics = pkg["aspects"]["economics"]["value"]
        self.assertEqual(economics["net_profit"], "NOT_VERIFIED")
        self.assertIn("不等于已核验净利润", economics["note"])
        self.assertIn("不得把收入直接当作净利润", economics["decisive_exception"])
        dims = {o["dimension"] for o in pkg["observations"]}
        self.assertEqual(dims, {"commercial", "cost"})
        boundaries = {b["observation_id"]: b for b in pkg.get("epistemic_boundaries", [])}
        self.assertFalse(boundaries["obs-sponsor-2"]["usable_as_verified_fact"])
        self.assertFalse(boundaries["obs-cost-1"]["usable_as_verified_fact"])
        self.record(
            "T8-P3", "收入、成本不得自动算作已核验净利润",
            {"intent": "profit"},
            {"net_profit": "NOT_VERIFIED", "dimensions": ["commercial", "cost"]},
            {"economics": economics, "dimensions": sorted(dims)},
        )

    def test_t8_context_envelope_has_no_duplicated_evidence_metadata(self):
        env = self.core.get_context(
            intent="profit",
            object_id="video/CREATOR-A-V001",
            scope="creator:CREATOR-A",
        )
        pkg = env["result"]["package"]
        for obs in pkg["observations"]:
            self.assertNotIn("source_refs", obs)
            self.assertNotIn("created_at", obs)
            self.assertIsInstance(obs["source_ref_ids"], list)
        self.assertNotIn("evidence", pkg)
        for entry in pkg["aspects"].values():
            self.assertNotIn("source_refs", entry)
            self.assertIsInstance(entry["source_ref_ids"], list)
        self.assertLess(env["result"]["package_chars"], 5000)
        # Authority, State and the decisive exception must survive compaction.
        self.assertEqual(env["authority"]["owner"], "user:owner")
        self.assertEqual(env["state"]["aspect"], "economics")
        self.assertIn("decisive_exception", pkg["aspects"]["economics"]["value"])
        self.record(
            "T8-P5", "Context envelope 去重证据元数据但保留 Authority/State/例外",
            {"intent": "profit"},
            {"no_embedded_source_refs": True, "no_package_evidence": True,
             "authority": "user:owner", "state": "economics"},
            {"package_chars": env["result"]["package_chars"],
             "observation_source_ids": [o["source_ref_ids"]
                                        for o in pkg["observations"]]},
        )

    def test_t8_cross_dimension_keeps_single_source_identity(self):
        env = self.core.get_context(
            intent="cross_dimension",
            object_id="video/CREATOR-A-V001",
            scope="creator:CREATOR-A",
        )
        pkg = env["result"]["package"]
        dims = {o["dimension"] for o in pkg["observations"]}
        self.assertEqual(dims, {"commercial", "tradeoff", "operations"})
        native_ids = {r["native_id"] for r in env["evidence"]}
        self.assertEqual(native_ids, {"sim-video-a-001"})
        self.record(
            "T8-P4", "跨维度解释仍指向同一视频身份与来源",
            {"intent": "cross_dimension"},
            {"dimensions": ["commercial", "tradeoff", "operations"],
             "native_id": "sim-video-a-001"},
            {"dimensions": sorted(dims), "native_ids": sorted(native_ids)},
        )

    def test_t8_negative_missing_timecode_not_fabricated(self):
        env = self.core.get_context(
            intent="sponsorship",
            object_id="video/CREATOR-A-V001",
            scope="creator:CREATOR-A",
        )
        anchors = {r["anchor"] for r in env["evidence"]}
        self.assertEqual(anchors, {"NO_TIMECODE"})
        self.assertTrue(any("TIMECODE_MISSING" in w for w in env["warnings"]))
        self.assertStatus(env, "PARTIAL", "T8 timecode")
        self.record(
            "T8-N1", "缺失字幕时间码不伪造锚点，结果标 PARTIAL",
            {"intent": "sponsorship"},
            {"anchors": ["NO_TIMECODE"], "status": "PARTIAL"},
            {"anchors": sorted(anchors), "warnings": env["warnings"]},
        )

    def test_t8_negative_author_claim_is_not_verified_fact(self):
        env = self.core.get_context(
            intent="production_stack",
            object_id="video/CREATOR-A-V001",
            scope="creator:CREATOR-A",
        )
        obs = {o["observation_id"]: o for o in env["result"]["package"]["observations"]}
        self.assertEqual(obs["obs-ai"]["stance"], "CASE")
        self.assertNotEqual(obs["obs-ai"]["stance"], "VERIFIED")
        boundaries = {b["observation_id"]: b for b in
                      env["result"]["package"].get("epistemic_boundaries", [])}
        self.assertFalse(boundaries["obs-ai"]["usable_as_verified_fact"])
        all_obs = self.store.get_observations("video/CREATOR-A-V001")
        verified = [o for o in all_obs if o["stance"] == "VERIFIED"]
        self.assertEqual([o["observation_id"] for o in verified], ["obs-published"])
        self.record(
            "T8-N2", "作者自述不得升格为独立核验事实",
            {"intent": "production_stack"},
            {"obs-ai": "CASE", "only_verified": "obs-published"},
            {"obs-ai_stance": obs["obs-ai"]["stance"],
             "verified": [o["observation_id"] for o in verified]},
        )


if __name__ == "__main__":
    unittest.main()
