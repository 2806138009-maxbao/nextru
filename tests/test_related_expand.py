"""nexus_related(expand_facts=True): neighbours' current facts inline, bounded, conflicts preserved."""

from __future__ import annotations

from helpers import NexusTest

from nexus.agent_tools import AgentTools
from nexus.ingest import FetchedDocument, ingest_document

SCOPE = "research:objects"
DOC = "项目｜记录\n一｜提案\n提案 A 已提交，报价 100。\n提案 B 状态有两种说法。\n"


class RelatedExpandTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        out = ingest_document(self.store, FetchedDocument("GOOGLE_DRIVE", "SIMOBJ", "项目记录", "1",
                                                          "2026-11-01T00:00:00Z", None, DOC), SCOPE,
                              object_id="doc/rec")
        ref = out["source_ref_id"]
        st = self.store
        with st.tx():
            st.add_object("project/p", "project", "示例项目", SCOPE)
            for oid, name in (("proposal/a", "提案 A"), ("proposal/b", "提案 B")):
                st.add_object(oid, "proposal", name, SCOPE)
                st.add_authority(f"auth/{oid}", "object", oid, None, "user", SCOPE,
                                 "GOOGLE_DRIVE:SIMOBJ", "2026-09-01T00:00:00Z", "PROPOSAL_ONLY", "MIRRORED")
                st.add_relation(f"rel/{oid}", oid, "PART_OF", "project/p", "UNVERIFIED", SCOPE, [ref])
            st.add_aspect("proposal/a:submission:r1", "proposal/a", "submission",
                          {"value": "SUBMITTED", "as_of": "2026-09-03", "anchor": "h1.1"},
                          "pilot.v1", "CURRENT", "UNVERIFIED", 1, True, None, None, ref)
            st.add_aspect("proposal/a:price:r1", "proposal/a", "price",
                          {"value": "100 fixed", "anchor": "h1.1"}, "pilot.v1", "CURRENT", "UNVERIFIED",
                          1, True, None, None, ref)
            st.add_aspect("proposal/b:submission:r1", "proposal/b", "submission",
                          {"value": None, "note": "conflicting records"}, "pilot.v1", "CURRENT",
                          "UNVERIFIED", 1, True, None, None, ref)
            st.add_conflict("conf/b", "proposal/b", "submission", "two records disagree",
                            [{"value": "SUBMITTED", "anchor": "h1.2"}, {"value": "NOT_SUBMITTED", "anchor": "h1.2"}],
                            [ref])
        self.tools = AgentTools(self.core, SCOPE)

    def test_default_is_unchanged(self) -> None:
        out = self.tools.nexus_related("project/p")
        self.assertEqual(len(out["edges"]), 2)
        self.assertTrue(all("other_facts" not in e for e in out["edges"]))

    def test_expand_inlines_facts_and_keeps_conflict(self) -> None:
        out = self.tools.nexus_related("project/p", expand_facts=True)
        facts = {e["other_id"]: {f["aspect"]: f for f in e["other_facts"]} for e in out["edges"]}
        self.assertEqual(set(facts), {"proposal/a", "proposal/b"})
        a = facts["proposal/a"]
        self.assertEqual(a["submission"]["value"]["value"], "SUBMITTED")
        self.assertEqual(a["submission"]["verification"], "UNVERIFIED")
        self.assertTrue(a["submission"]["sources"])
        b = facts["proposal/b"]["submission"]
        self.assertEqual(b["status"], "CONFLICTED")              # no single value served
        self.assertIsNone(b["value"])

    def test_max_facts_bounds_each_neighbour(self) -> None:
        out = self.tools.nexus_related("project/p", expand_facts=True, max_facts=1)
        self.assertTrue(all(len(e["other_facts"]) <= 1 for e in out["edges"]))
