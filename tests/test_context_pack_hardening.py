"""P1 Task 002 hardening tests for get_context_pack.

Covers scope authorisation, status/freshness propagation, stored object-type
routing, Authority binding, stable-ID validation, historical values, budget
serialisation and source revision identity.
"""

from __future__ import annotations

import json
import unittest

from helpers import NexusTest

from nexus.modes import _source_entries
from nexus.service import dispatch


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ContextPackHardening(NexusTest):
    def pack(self, **params):
        params.setdefault("mode", "CONCISE")
        return dispatch(self.core, "get_context_pack", params)

    def block(self, source_ref_id: str, status: str = "BLOCKED") -> None:
        self.conn.execute("UPDATE source_refs SET status = ? WHERE source_ref_id = ?",
                          (status, source_ref_id))
        self.conn.commit()

    # ---------------------------------------------------------------- scope
    def test_scope_is_not_inferred_from_object(self):
        env = self.pack(question="当前决定是什么？", object_id="project/DLC-01")
        self.assertStatus(env, "INVALID_SCOPE", "missing scope")
        self.assertIsNone(env["result"])

    def test_explicit_global_cannot_upgrade_to_scoped_read(self):
        env = self.pack(question="当前决定是什么？", object_id="project/DLC-01",
                        scope="global")
        self.assertStatus(env, "INVALID_SCOPE", "global upgrade")
        self.assertIsNone(env["result"])

    def test_wrong_scope_is_rejected(self):
        env = self.pack(question="当前决定是什么？", object_id="project/DLC-01",
                        scope="creative:first-video")
        self.assertStatus(env, "INVALID_SCOPE", "wrong scope")

    def test_detailed_global_cannot_read_scoped_observation(self):
        env = self.pack(object_id="creator/CREATOR-A", scope="global",
                        mode="DETAILED", item_ids=["obs-career"])
        self.assertStatus(env, "INVALID_SCOPE", "detailed global")
        self.assertIsNone(env["result"])

    def test_legal_scoped_call_still_works(self):
        env = self.pack(question="当前决定是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01")
        self.assertStatus(env, "OK", "legal scope")
        self.assertEqual(env["result"]["object"]["scope"], "project:DLC-01")

    # -------------------------------------------------------- status/freshness
    def test_blocked_context_is_not_published_as_ok(self):
        self.block("src/dlc-mission-r1")
        env = self.pack(question="当前决定和状态是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01")
        self.assertStatus(env, "BLOCKED", "blocked concise")
        self.assertEqual(env["result"]["status"], "BLOCKED")
        self.assertEqual(env["result"]["facts"], [])

    def test_missing_context_is_not_published_as_ok(self):
        self.block("src/dlc-mission-r1", "MISSING")
        env = self.pack(question="当前决定和状态是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01")
        self.assertStatus(env, "MISSING", "missing concise")
        self.assertEqual(env["result"]["status"], "MISSING")

    def test_historical_only_is_partial_and_keeps_history(self):
        env = self.pack(question="旧决定是什么？",
                        object_id="discussion/DLC-01-LEGACY-2026-09",
                        scope="project:DLC-01")
        self.assertStatus(env, "PARTIAL", "historical concise")
        self.assertEqual(env["result"]["status"], "PARTIAL")
        fact = env["result"]["facts"][0]
        self.assertEqual(fact["resolution"], "HISTORICAL_ONLY")
        self.assertTrue(fact["historical"])
        self.assertIn("采购新硬件", fact["statement"])

    def test_stale_snapshot_is_partial_and_marked(self):
        self.conn.execute("UPDATE source_heads SET latest_revision = 'r2' "
                          "WHERE native_id = 'sim-creative-doc'")
        self.conn.commit()
        env = self.pack(question="第一期视频的主题和排期是什么？",
                        object_id="creative/FIRST-VIDEO",
                        scope="creative:first-video")
        self.assertStatus(env, "PARTIAL", "snapshot concise")
        self.assertEqual(env["result"]["status"], "PARTIAL")
        self.assertTrue(any(f.get("resolution") == "SNAPSHOT_ONLY"
                            for f in env["result"]["facts"]))

    def test_detailed_blocked_is_blocked(self):
        self.block("src/dlc-mission-r1")
        env = self.pack(object_id="project/DLC-01", scope="project:DLC-01",
                        mode="DETAILED",
                        item_ids=["aspect:project/DLC-01/decision"])
        self.assertStatus(env, "BLOCKED", "blocked detailed")
        self.assertEqual(env["result"]["items"][0]["error"], "BLOCKED")

    def test_detailed_historical_is_partial_and_uses_historical_value(self):
        env = self.pack(object_id="discussion/DLC-01-LEGACY-2026-09",
                        scope="project:DLC-01", mode="DETAILED",
                        item_ids=["aspect:discussion/DLC-01-LEGACY-2026-09/decision"])
        self.assertStatus(env, "PARTIAL", "historical detailed")
        item = env["result"]["items"][0]
        self.assertTrue(item["historical"])
        self.assertEqual(item["resolution"], "HISTORICAL_ONLY")
        self.assertIn("采购新硬件", item["statement"])
        self.assertTrue(item["payload"])

    # ------------------------------------------------------------- type routing
    def test_creator_type_uses_creator_route_and_relations(self):
        env = self.pack(question="他的品牌合作、制作成本、设备和 AI 工作流是什么？",
                        object_id="creator/CREATOR-A", scope="creator:CREATOR-A")
        pack = env["result"]
        self.assertEqual(pack["object"]["kind"], "CREATOR")
        ids = {f["id"] for f in pack["facts"]}
        self.assertIn("obs-cost-1", ids)
        self.assertIn("obs-disclosure", ids)
        self.assertTrue(any(r["id"] == "rel-authored" and r["effective"]
                            for r in pack["relations"]))
        self.assertNotEqual(pack["status"], "OK", "timecode boundary must propagate")

    def test_project_type_routes_to_mission(self):
        env = self.pack(question="当前决定和状态是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01")
        pack = env["result"]
        self.assertEqual(pack["object"]["kind"], "MISSION")
        self.assertIn("aspect:project/DLC-01/decision",
                      {f["id"] for f in pack["facts"]})

    # ---------------------------------------------------------------- authority
    def test_authority_is_read_from_resolved_binding(self):
        env = self.pack(question="当前决定是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01")
        pack = env["result"]
        decision = next(f for f in pack["facts"] if f["id"].endswith("/decision"))
        self.assertEqual(decision["authority"]["owner"], "user:owner")
        self.assertEqual(decision["authority"]["binding_id"], "auth-decision")
        self.assertTrue(any(a["owner"] == "user:owner" for a in pack["authority"]))

    def test_detailed_authority_preserved(self):
        env = self.pack(object_id="project/DLC-01", scope="project:DLC-01",
                        mode="DETAILED",
                        item_ids=["aspect:project/DLC-01/decision"])
        item = env["result"]["items"][0]
        self.assertEqual(item["authority"]["owner"], "user:owner")
        self.assertEqual(item["authority"]["binding_id"], "auth-decision")
        self.assertEqual(item["as_of"], "2026-10-20T00:00:00Z")

    def test_unverified_authored_relation_is_not_used_for_evidence(self):
        self.conn.execute(
            "INSERT INTO objects(object_id,type,display_name,scope,created_at)"
            " VALUES('video/CREATOR-A-V002','Video','候选视频','creator:CREATOR-A','2026-08-13T00:00:00Z')")
        self.conn.execute(
            "INSERT INTO source_refs(source_ref_id,source_system,native_id,revision,location,"
            "anchor,access_scope,retrieved_at,status,content_sha256) VALUES"
            "('src/video-a-002','SIM','sim-video-a-002','v1','Drive/X','NO_TIMECODE',"
            "'creator:CREATOR-A','2026-08-13T00:00:00Z','OK',NULL)")
        self.conn.execute(
            "INSERT INTO observations(observation_id,object_id,dimension,statement,stance,"
            "detail,source_ref_ids,created_at) VALUES"
            "('obs-candidate','video/CREATOR-A-V002','commercial','候选视频不应进入上下文',"
            "'CASE',NULL,'[\"src/video-a-002\"]','2026-08-13T00:00:00Z')")
        self.conn.execute(
            "INSERT INTO relations(relation_id,from_id,relation_type,to_id,verification,"
            "scope,provenance_refs,valid_from,valid_to,created_at) VALUES"
            "('rel-authored-unverified','creator/CREATOR-A','AUTHORED',"
            "'video/CREATOR-A-V002','UNVERIFIED','creator:CREATOR-A','[]',NULL,NULL,"
            "'2026-08-13T00:00:00Z')")
        self.conn.commit()
        env = self.pack(question="他的品牌合作是什么？",
                        object_id="creator/CREATOR-A", scope="creator:CREATOR-A")
        pack = env["result"]
        self.assertNotIn("obs-candidate", {f["id"] for f in pack["facts"]})
        relation = next(r for r in pack["relations"]
                        if r["id"] == "rel-authored-unverified")
        self.assertFalse(relation["effective"])

    # --------------------------------------------------------- IDs / evidence
    def test_detailed_accepts_stored_observation_id(self):
        env = self.pack(object_id="creator/CREATOR-A", scope="creator:CREATOR-A",
                        mode="DETAILED", item_ids=["obs-career"])
        self.assertStatus(env, "OK", "observation id")
        self.assertEqual(env["result"]["items"][0]["kind"], "observation")

    def test_aspect_id_object_mismatch_is_rejected(self):
        env = self.pack(object_id="creator/CREATOR-A", scope="creator:CREATOR-A",
                        mode="DETAILED",
                        item_ids=["aspect:creative/FIRST-VIDEO/first_video"])
        self.assertStatus(env, "INVALID_SCOPE", "aspect mismatch")
        item = env["result"]["items"][0]
        self.assertEqual(item["error"], "INVALID_SCOPE")
        self.assertNotIn("payload", item)

    def test_source_entries_keep_revisions_separate(self):
        refs = [
            {"native_id": "doc-1", "revision": "r1", "anchor": "a1"},
            {"native_id": "doc-1", "revision": "r2", "anchor": "a2"},
            {"native_id": "doc-1", "revision": "r1", "anchor": "a3"},
        ]
        entries = _source_entries(refs)
        self.assertEqual(len(entries), 2)
        by_rev = {e["revision"]: e for e in entries}
        self.assertEqual(by_rev["r1"]["anchor_count"], 2)
        self.assertEqual(by_rev["r2"]["anchor_count"], 1)

    # ------------------------------------------------------------ as_of/budget
    def test_as_of_is_derived_from_aspect_metadata(self):
        env = self.pack(question="当前决定是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01")
        pack = env["result"]
        self.assertTrue(pack["as_of"])
        decision = next(f for f in pack["facts"] if f["id"].endswith("/decision"))
        self.assertEqual(decision["as_of"], "2026-10-20T00:00:00Z")

    def test_budget_chars_match_final_serialization(self):
        env = self.pack(question="第一期视频的主题和排期是什么？",
                        object_id="creative/FIRST-VIDEO",
                        scope="creative:first-video", max_chars=1200)
        pack = env["result"]
        actual = len(canonical(pack))
        self.assertEqual(pack["budget"]["chars"], actual)
        if not pack["budget"]["exceeded"]:
            self.assertLessEqual(actual, 1200)

    def test_budget_drop_is_partial_and_machine_readable(self):
        env = self.pack(question="当前决定和状态是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01", max_chars=1600)
        pack = env["result"]
        self.assertTrue(pack["budget"]["dropped"], "test must force a drop")
        self.assertFalse(pack["budget"]["exceeded"])
        self.assertTrue(pack["budget"]["truncated"])
        self.assertEqual(pack["status"], "PARTIAL")
        self.assertEqual(env["execution"]["status"], "PARTIAL")
        self.assertFalse(pack["completeness"]["complete"])
        self.assertEqual(pack["completeness"]["reason"], "BUDGET_DROPPED_FACTS")
        self.assertEqual(pack["completeness"]["dropped_facts"],
                         pack["budget"]["dropped"])
        self.assertEqual(pack["budget"]["chars"], len(canonical(pack)))

    def test_default_pack_is_complete(self):
        env = self.pack(question="当前决定是什么？", object_id="project/DLC-01",
                        scope="project:DLC-01")
        pack = env["result"]
        self.assertTrue(pack["completeness"]["complete"])
        self.assertIsNone(pack["completeness"]["reason"])
        self.assertFalse(pack["budget"]["truncated"])

    def test_unrelated_observation_is_rejected(self):
        self.conn.execute(
            "INSERT INTO objects(object_id,type,display_name,scope,created_at)"
            " VALUES('video/CREATOR-A-V002','Video','无关视频','creator:CREATOR-A',"
            "'2026-08-13T00:00:00Z')")
        self.conn.execute(
            "INSERT INTO source_refs(source_ref_id,source_system,native_id,revision,"
            "location,anchor,access_scope,retrieved_at,status,content_sha256) VALUES"
            "('src/video-a-002','SIM','sim-video-a-002','v1','Drive/X','NO_TIMECODE',"
            "'creator:CREATOR-A','2026-08-13T00:00:00Z','OK',NULL)")
        self.conn.execute(
            "INSERT INTO observations(observation_id,object_id,dimension,statement,stance,"
            "detail,source_ref_ids,created_at) VALUES"
            "('obs-unrelated','video/CREATOR-A-V002','commercial','不应返回的无关事实',"
            "'CASE',NULL,'[\"src/video-a-002\"]','2026-08-13T00:00:00Z')")
        self.conn.commit()
        env = self.pack(object_id="creator/CREATOR-A", scope="creator:CREATOR-A",
                        mode="DETAILED", item_ids=["obs-unrelated"])
        self.assertStatus(env, "INVALID_SCOPE", "unrelated observation")
        item = env["result"]["items"][0]
        self.assertEqual(item["error"], "INVALID_SCOPE")
        self.assertNotIn("statement", item)

    def test_pack_keeps_two_source_revisions(self):
        self.conn.execute(
            "INSERT INTO source_refs(source_ref_id,source_system,native_id,revision,"
            "location,anchor,access_scope,retrieved_at,status,content_sha256) VALUES"
            "('src/longdoc-style-r8','SIM','sim-creative-doc','r8','Drive/X',"
            "'§4-style-r8','creative:first-video','2026-10-30T00:00:00Z','OK',NULL)")
        self.conn.execute(
            "UPDATE aspects SET source_ref_id = 'src/longdoc-style-r8'"
            " WHERE object_id = 'creative/FIRST-VIDEO' AND name = 'style_quality'")
        self.conn.commit()
        env = self.pack(question="第一期视频的主题和风格是什么？",
                        object_id="creative/FIRST-VIDEO",
                        scope="creative:first-video")
        pack = env["result"]
        sources = pack["evidence"]["sources"]
        revisions = {s["revision"] for s in sources}
        self.assertIn("r7", revisions)
        self.assertIn("r8", revisions)
        by_rev = {s["revision"]: s for s in sources}
        self.assertEqual(by_rev["r7"]["anchor_count"], 1)
        self.assertEqual(by_rev["r8"]["anchor_count"], 1)
        self.assertEqual(pack["status"], "PARTIAL")

    def test_statement_is_identical_across_modes(self):
        row = self.conn.execute(
            "SELECT payload FROM aspects WHERE object_id = 'creative/FIRST-VIDEO'"
            " AND name = 'first_video'").fetchone()
        payload = json.loads(row["payload"])
        payload["statement"] = "长" * 600
        self.conn.execute(
            "UPDATE aspects SET payload = ? WHERE object_id = 'creative/FIRST-VIDEO'"
            " AND name = 'first_video'", (json.dumps(payload, ensure_ascii=False),))
        self.conn.commit()
        concise = self.pack(question="第一期视频的主题是什么？",
                            object_id="creative/FIRST-VIDEO",
                            scope="creative:first-video")
        fact = next(f for f in concise["result"]["facts"]
                    if f["id"].endswith("/first_video"))
        detailed = self.pack(object_id="creative/FIRST-VIDEO",
                             scope="creative:first-video", mode="DETAILED",
                             item_ids=["aspect:creative/FIRST-VIDEO/first_video"])
        item = detailed["result"]["items"][0]
        self.assertEqual(fact["statement"], item["statement"])
        self.assertEqual(len(fact["statement"]), 600)


if __name__ == "__main__":
    unittest.main()
