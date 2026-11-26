"""Task 008 S3 — extracted candidates: verbatim evidence, entity resolution, UNVERIFIED only."""

from __future__ import annotations

import json

from helpers import NexusTest

from nexus.errors import Status
from nexus.ingest import FetchedDocument, import_candidates, ingest_document, split_sections
from nexus.service import dispatch

SCOPE = "research:t008c"
DOC_A = "创作者案例｜A\n一｜器材\n作者 @小黑 先用手机，再买灯光。\n赞助方是石头科技。\n"
DOC_B = "影像研究｜B\n一｜参考\n作者 @ami.moment 用 iPhone 拍摄。\n她说灯光只是点缀。\n"


def doc(text: str, doc_id: str) -> FetchedDocument:
    return FetchedDocument("GOOGLE_DRIVE", doc_id, doc_id, "1", None, None, text)


def anchor(text: str, needle: str) -> str:
    return next(s.anchor for s in split_sections(text)[1] if needle in s.body)


REF_A = "src/google-drive/SIMCANDA@v1"
REF_B = "src/google-drive/SIMCANDB@v1"


class CandidateImportTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        ingest_document(self.store, doc(DOC_A, "SIMCANDA"), SCOPE, object_id="doc/a")
        ingest_document(self.store, doc(DOC_B, "SIMCANDB"), SCOPE, object_id="doc/b")
        self.a1 = anchor(DOC_A, "手机")
        self.a2 = anchor(DOC_A, "石头科技")
        self.b1 = anchor(DOC_B, "iPhone")
        self.b2 = anchor(DOC_B, "点缀")

    def payload(self, **over) -> dict:
        base = {
            "run_id": "test-run",
            "entities": [
                {"key": "xh", "name": "小黑", "type": "creator", "aliases": ["@小黑"],
                 "mentions": [{"source_ref_id": REF_A, "anchor": self.a1, "quote": "作者 @小黑"}]},
                {"key": "ami", "name": "ami.moment", "type": "creator",
                 "mentions": [{"source_ref_id": REF_B, "anchor": self.b1,
                               "quote": "作者 @ami.moment"}]},
                {"key": "light", "name": "灯光", "type": "concept",
                 "mentions": [{"source_ref_id": REF_A, "anchor": self.a1, "quote": "再买灯光"},
                              {"source_ref_id": REF_B, "anchor": self.b2, "quote": "灯光只是点缀"}]},
            ],
            "relations": [
                {"from": "xh", "to": "light", "type": "PRIORITISES", "source_ref_id": REF_A,
                 "anchor": self.a1, "quote": "先用手机，再买灯光"},
                {"from": "ami", "to": "light", "type": "TREATS_AS_ACCENT", "source_ref_id": REF_B,
                 "anchor": self.b2, "quote": "灯光只是点缀"},
            ],
        }
        base.update(over)
        return base

    def test_accepts_valid_candidates_as_unverified(self) -> None:
        out = import_candidates(self.store, self.payload(), SCOPE)
        self.assertEqual(out["counts"]["rejected"], 0, out["rejected"])
        self.assertEqual(out["counts"]["relations_new"], 2)
        rows = self.conn.execute("SELECT verification, extraction_run FROM relations"
                                 " WHERE scope = ?", (SCOPE,)).fetchall()
        self.assertEqual({tuple(r) for r in rows}, {("UNVERIFIED", "test-run")})
        # the shared concept links the two documents: a cross-document path exists
        light = next(e["object_id"] for e in out["accepted"]["entities"] if e["key"] == "light")
        env = dispatch(self.core, "traverse_relations", {
            "object_id": light, "scope": SCOPE, "direction": "both", "include_candidates": True})
        self.assertStatus(env, Status.OK)
        self.assertEqual(len(env["result"]["edges"]), 2)

    def test_candidates_hidden_without_include_flag(self) -> None:
        import_candidates(self.store, self.payload(), SCOPE)
        env = dispatch(self.core, "traverse_relations", {
            "object_id": "creator/小黑", "scope": SCOPE, "direction": "both"})
        self.assertEqual(env["result"]["edges"], [])
        self.assertEqual(env["result"]["candidate_count"], 1)

    def test_fabricated_quote_and_bad_anchor_rejected(self) -> None:
        rels = [
            {"from": "xh", "to": "light", "type": "PRIORITISES", "source_ref_id": REF_A,
             "anchor": self.a1, "quote": "先买麦克风"},                      # not in text
            {"from": "xh", "to": "light", "type": "PRIORITISES", "source_ref_id": REF_A,
             "anchor": "h9.9-deadbeef", "quote": "再买灯光"},                 # bad anchor
            {"from": "xh", "to": "light", "type": "PRIORITISES", "source_ref_id": REF_A,
             "anchor": self.a2, "quote": "再买灯光"},                         # wrong paragraph
            {"from": "xh", "to": "light", "type": "prioritises", "source_ref_id": REF_A,
             "anchor": self.a1, "quote": "再买灯光"},                         # bad type
            {"from": "xh", "to": "xh", "type": "SAME", "source_ref_id": REF_A,
             "anchor": self.a1, "quote": "再买灯光"},                         # self edge
            {"from": "nobody", "to": "light", "type": "X_Y", "source_ref_id": REF_A,
             "anchor": self.a1, "quote": "再买灯光"},                         # unresolved
        ]
        out = import_candidates(self.store, self.payload(relations=rels), SCOPE)
        reasons = [r["reason"] for r in out["rejected"]]
        self.assertEqual(reasons, ["QUOTE_NOT_IN_SECTION", "ANCHOR_NOT_FOUND",
                                   "QUOTE_NOT_IN_SECTION", "RELATION_TYPE_INVALID",
                                   "SELF_RELATION", "FROM_UNRESOLVED"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM relations WHERE scope=?",
                                           (SCOPE,)).fetchone()[0], 0)

    def test_legacy_anchor_alias_is_accepted_and_normalised(self) -> None:
        legacy = next(s.legacy for s in split_sections(DOC_A)[1] if "手机" in s.body)
        rel = {"from": "xh", "to": "light", "type": "PRIORITISES", "source_ref_id": REF_A,
               "anchor": legacy, "quote": "再买灯光"}
        out = import_candidates(self.store, self.payload(relations=[rel]), SCOPE)
        self.assertEqual(out["counts"]["relations_new"], 1)
        row = self.conn.execute("SELECT detail FROM observations WHERE dimension='relation_evidence'"
                                ).fetchone()
        self.assertEqual(json.loads(row[0])["anchor"], self.a1)

    def test_alias_reuses_existing_entity_and_ambiguity_is_rejected(self) -> None:
        import_candidates(self.store, self.payload(), SCOPE)
        again = {"key": "xh2", "name": "＠小黑", "type": "creator",
                 "mentions": [{"source_ref_id": REF_A, "anchor": self.a1, "quote": "作者 @小黑"}]}
        out = import_candidates(self.store, self.payload(entities=[again], relations=[]), SCOPE)
        self.assertEqual(out["accepted"]["entities"][0]["object_id"], "creator/小黑")
        self.assertFalse(out["accepted"]["entities"][0]["created"])
        # make a second object answer to the same alias -> ambiguous
        self.store.add_object("creator/xiaohei-2", "creator", "小黑", SCOPE)
        out = import_candidates(self.store, self.payload(entities=[again], relations=[]), SCOPE)
        self.assertTrue(out["rejected"][0]["reason"].startswith("ENTITY_AMBIGUOUS"))

    def test_foreign_scope_source_rejected(self) -> None:
        ingest_document(self.store, doc("其他｜C\n作者 @小黑 也在这里。\n", "SIMCANDC"),
                        "research:elsewhere", object_id="doc/c")
        ref_c = "src/google-drive/SIMCANDC@v1"
        bad = {"key": "x", "name": "小黑", "type": "creator",
               "mentions": [{"source_ref_id": ref_c,
                             "anchor": anchor("其他｜C\n作者 @小黑 也在这里。\n", "也在"),
                             "quote": "作者 @小黑"}]}
        out = import_candidates(self.store, self.payload(entities=[bad], relations=[]), SCOPE)
        self.assertEqual(out["rejected"][0]["reason"], "SOURCE_SCOPE_DENIED")

    def test_merge_adds_provenance_and_supersede_expires_old_edge(self) -> None:
        first = import_candidates(self.store, self.payload(), SCOPE)
        old_id = first["accepted"]["relations"][0]
        dup = {"from": "xh", "to": "light", "type": "PRIORITISES", "source_ref_id": REF_B,
               "anchor": self.b2, "quote": "灯光只是点缀"}
        out = import_candidates(self.store, self.payload(relations=[dup]), SCOPE)
        self.assertEqual(out["accepted"]["merged"], [old_id])
        refs = json.loads(self.store.get_relation(old_id)["provenance_refs"])
        self.assertEqual(sorted(refs), sorted([REF_A, REF_B]))
        new = {"from": "xh", "to": "light", "type": "DEPRIORITISES", "source_ref_id": REF_A,
               "anchor": self.a1, "quote": "先用手机", "supersedes": old_id}
        out = import_candidates(self.store, self.payload(relations=[new]), SCOPE)
        old = self.store.get_relation(old_id)
        self.assertIsNotNone(old["expired_at"])
        self.assertEqual(old["superseded_by"], out["accepted"]["relations"][0])
        env = dispatch(self.core, "list_relation_types", {"scope": SCOPE})
        types = {t["relation_type"] for t in env["result"]["relation_types"]}
        self.assertNotIn("PRIORITISES", types)
        self.assertIn("DEPRIORITISES", types)

    def test_nothing_becomes_current_or_verified(self) -> None:
        import_candidates(self.store, self.payload(), SCOPE)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE scope=? AND verification='VERIFIED'",
            (SCOPE,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM aspects a JOIN objects o ON o.object_id=a.object_id"
            " WHERE o.scope=? AND a.is_current=1", (SCOPE,)).fetchone()[0], 0)
