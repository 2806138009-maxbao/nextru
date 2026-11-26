"""T2 — Relation traversal: forward/reverse 1–2 hops, source move, unverified
edges must never masquerade as effective relations. Contract refs: P-03, R2, INV-04."""

from __future__ import annotations

import unittest

from helpers import NexusTest, summarize


class T2Relations(NexusTest):
    def test_t2_positive_forward_relation(self):
        env = self.core.traverse_relations(
            "project/DLC-01", relation_types=["GOVERNED_BY"], direction="out",
            max_depth=1, scope="project:DLC-01",
        )
        self.assertStatus(env, "OK", "T2 forward")
        edges = env["result"]["edges"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["to_id"], "principle/P-01")
        self.assertTrue(edges[0]["effective"])
        self.assertEqual(edges[0]["verification"], "VERIFIED")
        self.record(
            "T2-P1", "项目 → 适用原则正向查询",
            {"start": "project/DLC-01", "type": "GOVERNED_BY", "direction": "out"},
            {"to": "principle/P-01", "effective": True},
            summarize(env),
        )

    def test_t2_positive_reverse_and_two_hops(self):
        reverse = self.core.traverse_relations(
            "principle/P-01", relation_types=["GOVERNED_BY"], direction="in",
            max_depth=1, scope="project:DLC-01",
        )
        self.assertStatus(reverse, "OK", "T2 reverse")
        self.assertIn("project/DLC-01", [e["other_object"]["object_id"]
                                         for e in reverse["result"]["edges"]])

        two = self.core.traverse_relations(
            "principle/P-01", relation_types=["APPLIES_TO", "DEPENDS_ON"],
            direction="both", max_depth=2, scope="project:DLC-01",
        )
        self.assertStatus(two, "OK", "T2 two hops")
        reached = {(e["other_object"]["object_id"], e["depth"])
                   for e in two["result"]["edges"]}
        self.assertIn(("project/DLC-01", 1), reached)
        self.assertIn(("course/DEMO0009", 2), reached)
        self.record(
            "T2-P2", "原则 → 项目反向 + 2 跳到达课程对象",
            {"start": "principle/P-01", "max_depth": 2},
            {"depth1": "project/DLC-01", "depth2": "course/DEMO0009"},
            summarize(two),
        )

    def test_t2_source_move_keeps_object_and_relation(self):
        before = self.core.traverse_relations(
            "project/DLC-01", relation_types=["GOVERNED_BY"], direction="out",
            max_depth=1, scope="project:DLC-01",
        )
        self.store.move_source_ref(
            "src/dlc-principle-r1",
            "Drive/Archive/2026/Principle-renamed-and-moved.md",
            "§1 原则（移动后）",
        )
        after = self.core.traverse_relations(
            "project/DLC-01", relation_types=["GOVERNED_BY"], direction="out",
            max_depth=1, scope="project:DLC-01",
        )
        self.assertEqual(before["result"]["edges"][0]["relation_id"],
                         after["result"]["edges"][0]["relation_id"])
        trace = self.core.trace_evidence("rel-governed", scope="project:DLC-01")
        ref = trace["evidence"][0]
        self.assertEqual(ref["native_id"], "sim-dlc-principle")
        self.assertIn("Principle-renamed-and-moved", ref["location"])
        resolved = self.core.resolve_object("sim-dlc-principle", scope="project:DLC-01")
        self.assertEqual(resolved["result"]["object"]["object_id"], "principle/P-01")
        self.record(
            "T2-P3", "文件改名/移动只改变 SourceRef 定位，不破坏稳定 Object ID",
            {"move": "src/dlc-principle-r1 -> Drive/Archive/.../renamed"},
            {"relation_id": "rel-governed", "object_id": "principle/P-01",
             "native_id": "sim-dlc-principle"},
            {"trace_ref": ref, "resolved": resolved["result"]["object"]["object_id"]},
        )

    def test_t2_negative_unverified_relation_not_effective(self):
        default = self.core.traverse_relations(
            "project/DLC-01", relation_types=["RELATED_TO"], direction="out",
            max_depth=1, scope="project:DLC-01",
        )
        self.assertStatus(default, "UNKNOWN", "T2 unverified excluded")
        self.assertEqual(default["result"]["edges"], [])
        self.assertEqual(default["result"]["candidate_count"], 1)
        self.assertTrue(any("unverified" in w for w in default["warnings"]))

        with_candidates = self.core.traverse_relations(
            "project/DLC-01", relation_types=["RELATED_TO"], direction="out",
            max_depth=1, scope="project:DLC-01", include_candidates=True,
        )
        edge = with_candidates["result"]["edges"][0]
        self.assertTrue(edge["candidate"])
        self.assertFalse(edge["effective"])
        self.assertEqual(edge["verification"], "UNVERIFIED")
        self.record(
            "T2-N1", "未经核实的 Relation 不得伪装成正式有效关系",
            {"start": "project/DLC-01", "type": "RELATED_TO"},
            {"default": "excluded", "as_candidate": {"effective": False,
                                                     "candidate": True}},
            {"default": default["result"]["candidate_count"],
             "candidate_edge": edge},
        )

    def test_t2_negative_scope_isolation(self):
        env = self.core.traverse_relations(
            "project/DLC-01", direction="out", max_depth=1, scope="course:DEMO0009"
        )
        self.assertStatus(env, "INVALID_SCOPE", "T2 cross-scope")
        self.record(
            "T2-N2", "跨 scope 遍历被拒绝",
            {"scope": "course:DEMO0009"},
            {"status": "INVALID_SCOPE"},
            summarize(env),
        )


if __name__ == "__main__":
    unittest.main()
