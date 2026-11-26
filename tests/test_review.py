"""Tiered owner attention for agent relations/entities: AUTO filing, QUIET candidates, GATE queue."""

from __future__ import annotations

import json
import unittest

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus import review
from nexus import search as si
from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store, connect, init_schema
from nexus.native import NativeDocs
from test_native_v0 import SCOPE, fresh_db

A = "agent:ds"


class Review(unittest.TestCase):
    def setUp(self):
        self.path = fresh_db()
        self.conn = connect(self.path)
        init_schema(self.conn)
        self.conn.execute(
            "INSERT INTO observations(observation_id, object_id, dimension, statement, stance, detail,"
            " source_ref_ids, created_at) VALUES ('obs/doc/mirror1@v3/0003','doc/mirror1','document_paragraph',"
            " 'Decision: return on 12-08.', 'CASE', ?, '[\"ref-m1\"]', '2026-11-08')",
            (json.dumps({"anchor": "h0.3-abcd1234"}),))
        self.conn.commit()
        si.ensure_index(self.conn)
        self.conn.commit()
        self.agent = NativeDocs(self.conn, agent_writes=True)
        self.owner = NativeDocs(self.conn)
        self.doc = self.agent.create(SCOPE, {"title": "2026-11-10 training", "children": ["lat pulldown 40kg"]},
                                     A)["doc_id"]
        self.node = self.agent.read(self.doc)["nodes"][0]["node_id"]
        self.doc_ev = [{"doc_id": self.doc, "node_id": self.node}]
        self.mirror_ev = [{"object_id": "doc/mirror1", "anchor": "h0.3-abcd1234"}]
        self.log = self.agent.create_object(SCOPE, "training_log", "Training log", A, evidence=self.doc_ev)
        self.decision = self.agent.create_object(SCOPE, "decision", "Return on 12-08", A, evidence=self.mirror_ev)

    def tearDown(self):
        self.conn.close()

    def status(self, pid):
        return tuple(self.conn.execute("SELECT status, decided_by FROM native_proposals WHERE proposal_id = ?",
                                       (pid,)).fetchone())

    def test_plain_filing_is_confirmed_by_policy_and_undoable(self):
        out = self.agent.add_relation(SCOPE, self.doc, "PART_OF", self.log["object_id"], A, evidence=self.doc_ev)
        self.assertEqual((out["review"], out["verification"]), ("AUTO", "VERIFIED"))
        self.assertEqual(self.status(out["proposal_id"]), ("ACCEPTED", review.POLICY_ACTOR))
        row = self.conn.execute("SELECT verification FROM relations WHERE relation_id = ?",
                                (out["relation_id"],)).fetchone()
        self.assertEqual(row[0], "VERIFIED")
        # readers see that the policy, not the owner, confirmed it
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        e = next(x for x in tools.nexus_read(self.log["object_id"])["edges"] if x["relation_id"] == out["relation_id"])
        self.assertEqual(e["confirmed_by"], "policy (plain filing)")
        e = next(x for x in tools.nexus_related(self.doc)["edges"] if x["relation_id"] == out["relation_id"])
        self.assertEqual(e["confirmed_by"], "policy (plain filing)")
        # not in any queue, and undone like any relation
        self.assertNotIn(out["proposal_id"], [p["proposal_id"] for p in
                                              self.owner.proposals("PROPOSED", "GATE")["proposals"]])
        self.assertEqual(self.agent.remove_relation(out["relation_id"], A)["status"], "OK")

    def test_filing_escalates_when_authority_or_evidence_elsewhere(self):
        # filing onto a decision: a candidate, not auto-confirmed
        out = self.agent.add_relation(SCOPE, self.doc, "about", self.decision["object_id"], A, evidence=self.doc_ev)
        self.assertEqual((out["review"], out["verification"]), ("QUIET", "UNVERIFIED"))
        # filing whose evidence lives in neither endpoint
        other = self.agent.create(SCOPE, {"title": "other", "children": ["x"]}, A)["doc_id"]
        out = self.agent.add_relation(SCOPE, other, "PART_OF", self.log["object_id"], A, evidence=self.mirror_ev)
        self.assertEqual(out["review"], "QUIET")

    def test_decision_changing_relations_wait_in_the_queue(self):
        gate = self.agent.add_relation(SCOPE, self.decision["object_id"], "CONFLICTS_WITH", "doc/mirror1", A,
                                       evidence=self.mirror_ev)
        novel = self.agent.add_relation(SCOPE, self.decision["object_id"], "WEAKENS", self.log["object_id"], A,
                                        evidence=self.mirror_ev)
        quiet = self.agent.add_relation(SCOPE, self.decision["object_id"], "BASED_ON", "doc/mirror1", A,
                                        evidence=self.mirror_ev)
        self.assertEqual([gate["review"], novel["review"], quiet["review"]], ["GATE", "GATE", "QUIET"])
        for o in (gate, novel, quiet):
            self.assertEqual(o["verification"], "UNVERIFIED")
            self.assertEqual(self.status(o["proposal_id"]), ("PROPOSED", None))
        queue = {p["proposal_id"] for p in self.owner.proposals("PROPOSED", "GATE")["proposals"]}
        self.assertEqual(queue & {gate["proposal_id"], novel["proposal_id"], quiet["proposal_id"]},
                         {gate["proposal_id"], novel["proposal_id"]})
        # agent-made decision entities are queued; an ordinary collection entity is not
        self.assertIn(self.decision["proposal_id"], queue)
        self.assertNotIn(self.log["proposal_id"], queue)
        # readers see which candidates are interpretations to check against the source before acting
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        marks = {e["relation_id"]: e.get("check_source") for e in
                 tools.nexus_related(self.decision["object_id"])["edges"]}
        self.assertEqual((marks[gate["relation_id"]], marks[novel["relation_id"]], marks[quiet["relation_id"]]),
                         (True, True, None))
        # quiet candidates can still be confirmed by the owner
        self.assertEqual(self.owner.accept(quiet["proposal_id"])["verification"], "VERIFIED")

    def test_hyperlinks_and_drive_folders_are_plain_filing(self):
        self.conn.execute("INSERT INTO objects(object_id, type, display_name, scope, created_at)"
                          " VALUES ('doc/mirror2', 'document', 'Other doc', ?, '2026-11-08')", (SCOPE,))
        link = self.agent.add_relation(SCOPE, "doc/mirror1", "LINKS_TO", "doc/mirror2", A, evidence=["ref-m1"])
        self.assertEqual(link["review"], "QUIET")      # ref-m1 does not name either endpoint
        self.conn.execute("INSERT INTO source_refs(source_ref_id, source_system, native_id, revision, location,"
                          " anchor, access_scope, retrieved_at, status, content_sha256) VALUES"
                          " ('src/google-drive-folder/F1', 'GOOGLE_DRIVE', 'F1', 'meta', 'https://drive', "
                          "'folder metadata', ?, '2026-11-10', 'OK', 'x')", (SCOPE,))
        folder = self.agent.create_object(SCOPE, "folder", "01｜EXU Learning", A,
                                          evidence=["src/google-drive-folder/F1"])
        self.assertEqual((folder["review"], folder["verification"]), ("AUTO", "VERIFIED"))
        self.assertEqual(self.status(folder["proposal_id"]), ("ACCEPTED", review.POLICY_ACTOR))
        part = self.agent.add_relation(SCOPE, "doc/mirror2", "PART_OF", folder["object_id"], A,
                                       evidence=["src/google-drive-folder/F1"])
        self.assertEqual((part["review"], part["verification"]), ("AUTO", "VERIFIED"))
        # a folder is not a free pass: an agent's folder without Drive metadata stays a candidate
        bare = self.agent.create_object(SCOPE, "folder", "My idea", A, evidence=self.mirror_ev)
        self.assertEqual(bare["review"], "QUIET")

    def test_untiered_old_proposals_stay_in_the_queue(self):
        self.conn.execute("INSERT INTO native_proposals(proposal_id, kind, payload, evidence, proposer, created_at, status)"
                          " VALUES ('prop_old', 'RELATION', '{\"relation_type\":\"X\"}', '[]', 'agent:ds', '2026-11-09', 'PROPOSED')")
        self.assertIn("prop_old", [p["proposal_id"] for p in self.owner.proposals("PROPOSED", "GATE")["proposals"]])


if __name__ == "__main__":
    unittest.main()
