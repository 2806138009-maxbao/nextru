"""CONCISE / DETAILED context delivery tests (P1 LIMITED_REFINE)."""

from __future__ import annotations

import unittest

from helpers import NexusTest

from nexus.service import dispatch


class ContextPackModes(NexusTest):
    object_id = "creative/FIRST-VIDEO"
    scope = "creative:first-video"
    question = "第一条视频的主题、拍摄和排期是什么？"

    def concise(self, **overrides):
        params = {
            "question": self.question,
            "object_id": self.object_id,
            "scope": self.scope,
            "mode": "CONCISE",
        }
        params.update(overrides)
        return dispatch(self.core, "get_context_pack", params)

    def test_concise_is_default_and_small(self):
        env = dispatch(self.core, "get_context_pack", {
            "question": self.question, "object_id": self.object_id,
            "scope": self.scope})
        self.assertStatus(env, "OK", "concise default")
        pack = env["result"]
        self.assertEqual(pack["mode"], "CONCISE")
        self.assertTrue(pack["facts"], "concise must return at least one fact")
        self.assertIn("Nexus", str(pack["facts"]))
        self.assertNotIn("evidence", env, "minimal envelope must not dump full evidence")
        self.assertLessEqual(len(str(pack)), 6000)
        self.assertEqual(env["execution"]["provider_io"]["drive_api_calls"], 0)

    def test_detailed_expands_the_same_fact_without_contradiction(self):
        concise = self.concise()
        fact = concise["result"]["facts"][0]
        detailed = dispatch(self.core, "get_context_pack", {
            "object_id": self.object_id,
            "scope": self.scope,
            "mode": "DETAILED",
            "item_ids": [fact["id"]],
        })
        self.assertStatus(detailed, "OK", "detailed")
        pack = detailed["result"]
        self.assertEqual(pack["mode"], "DETAILED")
        self.assertEqual(len(pack["items"]), 1)
        item = pack["items"][0]
        self.assertEqual(item["id"], fact["id"])
        self.assertEqual(item["statement"], fact["statement"],
                         "DETAILED must not contradict CONCISE")
        self.assertTrue(item.get("source_refs"), "detailed must expose SourceRefs")
        self.assertEqual(item["content_availability"]["source_body"], "UNAVAILABLE")

    def test_unknown_mode_is_rejected(self):
        env = self.concise(mode="VERBOSE")
        self.assertStatus(env, "INVALID_SCOPE", "mode validation")
        self.assertIn("allowed_modes", env["error"]["detail"])

    def test_detailed_requires_item_ids(self):
        env = dispatch(self.core, "get_context_pack", {
            "object_id": self.object_id, "scope": self.scope, "mode": "DETAILED"})
        self.assertStatus(env, "INVALID_SCOPE", "item_ids required")

    def test_unknown_object_is_not_found(self):
        env = dispatch(self.core, "get_context_pack", {
            "question": "anything", "object_id": "missing/OBJECT",
            "scope": "creative:first-video"})
        self.assertStatus(env, "NOT_FOUND", "unknown object")


if __name__ == "__main__":
    unittest.main()
