"""Task 007 — navigation fixes: object catalog, relation vocabulary, counts.

These are the three defects a cold-start agent hit in Task 006:
  * no way to enumerate objects without knowing an id;
  * no way to learn the valid relation types before traversing;
  * `candidate_count` reported 0 while candidate edges were being returned.
"""

from __future__ import annotations

import json

from helpers import NexusTest

from nexus.errors import Status
from nexus.service import dispatch

DLC = "project/DLC-01"
DLC_SCOPE = "project:DLC-01"


class ObjectCatalogTests(NexusTest):

    def test_lists_objects_in_scope_without_payloads(self) -> None:
        env = dispatch(self.core, "list_objects", {"scope": DLC_SCOPE})
        self.assertStatus(env, Status.OK)
        result = env["result"]
        ids = {o["object_id"] for o in result["objects"]}
        self.assertIn(DLC, ids)
        self.assertIn("principle/P-01", ids)
        for obj in result["objects"]:
            self.assertEqual(set(obj), {"object_id", "type", "display_name"})
        self.assertEqual(result["count"], len(result["objects"]))
        self.assertIn("content_withheld", result)

    def test_type_filter(self) -> None:
        env = dispatch(self.core, "list_objects",
                       {"scope": DLC_SCOPE, "object_type": "Project"})
        self.assertStatus(env, Status.OK)
        self.assertTrue(env["result"]["objects"])
        self.assertEqual({o["type"] for o in env["result"]["objects"]}, {"Project"})

    def test_limit_and_truncation(self) -> None:
        env = dispatch(self.core, "list_objects", {"scope": DLC_SCOPE, "limit": 1})
        self.assertStatus(env, Status.OK)
        self.assertEqual(len(env["result"]["objects"]), 1)
        self.assertTrue(env["result"]["truncated"])

    def test_global_cannot_enumerate_scoped_objects(self) -> None:
        env = dispatch(self.core, "list_objects", {"scope": "global"})
        self.assertStatus(env, Status.OK)
        self.assertEqual(env["result"]["objects"], [])

    def test_unknown_scope_is_rejected(self) -> None:
        env = dispatch(self.core, "list_objects", {"scope": "nope"})
        self.assertStatus(env, Status.INVALID_SCOPE)

    def test_unknown_parameter_is_rejected(self) -> None:
        env = dispatch(self.core, "list_objects",
                       {"scope": DLC_SCOPE, "dump": True})
        self.assertStatus(env, Status.INVALID_SCOPE)


class RelationVocabularyTests(NexusTest):

    def test_lists_relation_types_without_endpoints(self) -> None:
        env = dispatch(self.core, "list_relation_types", {"scope": DLC_SCOPE})
        self.assertStatus(env, Status.OK)
        types = {t["relation_type"] for t in env["result"]["relation_types"]}
        self.assertIn("RELATED_TO", types)
        self.assertIn("GOVERNED_BY", types)
        blob = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("relation_id", blob)
        self.assertNotIn("from_id", blob)
        self.assertNotIn("to_id", blob)
        for entry in env["result"]["relation_types"]:
            self.assertIn("by_verification", entry)

    def test_scope_isolation(self) -> None:
        env = dispatch(self.core, "list_relation_types",
                       {"scope": "creator:CREATOR-A"})
        self.assertStatus(env, Status.OK)
        self.assertNotIn("GOVERNED_BY",
                         {t["relation_type"] for t in env["result"]["relation_types"]})


class CandidateCountTests(NexusTest):

    def _traverse(self, include: bool) -> dict:
        return self.core.traverse_relations(
            DLC, relation_types=["RELATED_TO"], direction="out", max_depth=1,
            scope=DLC_SCOPE, include_candidates=include)["result"]

    def test_count_is_total_candidates_when_candidates_excluded(self) -> None:
        result = self._traverse(False)
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["included_candidates"], 0)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(len(result["excluded_candidates"]), 1)

    def test_count_matches_edges_when_candidates_included(self) -> None:
        result = self._traverse(True)
        self.assertEqual(len(result["edges"]), 1)
        self.assertTrue(result["edges"][0]["candidate"])
        self.assertEqual(result["included_candidates"], 1)
        self.assertEqual(result["excluded_candidates"], [])
        # Regression: this used to be 0 while a candidate edge was returned.
        self.assertEqual(result["candidate_count"], 1)

    def test_count_zero_when_no_candidates(self) -> None:
        result = self.core.traverse_relations(
            DLC, relation_types=["GOVERNED_BY"], direction="out", max_depth=1,
            scope=DLC_SCOPE, include_candidates=True)["result"]
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["included_candidates"], 0)
