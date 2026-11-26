"""Task 008 S2 — full-text discovery (FTS5 trigram) with scope isolation."""

from __future__ import annotations

from helpers import NexusTest

from nexus.errors import Status
from nexus.ingest import FetchedDocument, ingest_document
from nexus.service import dispatch

SCOPE = "research:t008"
OTHER = "research:t008-other"

TEXT = """检索测试｜合成文档
一｜器材顺序
先用手机拍摄，再买灯光，麦克风排在灯光之后。
二｜画面语法
章节标题 UI 放在左上角，配一条蓝色竖线。
""" + "很长的段落" * 80 + "\n"


def make(text: str, doc_id: str) -> FetchedDocument:
    return FetchedDocument("GOOGLE_DRIVE", doc_id, doc_id, "1", None, None, text)


class SearchTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        ingest_document(self.store, make(TEXT, "SIMSEARCH1"), SCOPE, object_id="doc/search")
        ingest_document(self.store, make("别的范围｜文档\n灯光只在这里出现。\n", "SIMSEARCH2"), OTHER,
                        object_id="doc/other")

    def q(self, query: str, scope: str = SCOPE, **extra) -> dict:
        return dispatch(self.core, "search", {"query": query, "scope": scope, **extra})

    def test_chinese_two_char_term_hits_paragraph_with_anchor(self) -> None:
        env = self.q("灯光")
        self.assertStatus(env, Status.OK)
        hit = env["result"]["hits"][0]
        self.assertEqual(hit["kind"], "paragraph")
        self.assertEqual(hit["object_id"], "doc/search")
        self.assertTrue(hit["anchor"].startswith("h1.1-"))
        self.assertIn("灯光", hit["snippet"])

    def test_natural_language_question_matches_via_bigrams(self) -> None:
        env = self.q("麦克风和灯光哪个先买")
        self.assertStatus(env, Status.OK)
        self.assertTrue(env["result"]["hits"][0]["anchor"].startswith("h1.1-"))

    def test_scope_isolation(self) -> None:
        ids = {h["object_id"] for h in self.q("灯光")["result"]["hits"]}
        self.assertNotIn("doc/other", ids)
        other = {h["object_id"] for h in self.q("灯光", OTHER)["result"]["hits"]}
        self.assertEqual(other, {"doc/other"})
        glob = self.q("灯光", "global")
        self.assertStatus(glob, Status.NOT_FOUND)

    def test_snippet_never_returns_full_body(self) -> None:
        env = self.q("很长的段落")
        for hit in env["result"]["hits"]:
            self.assertLessEqual(len(hit["snippet"]), 145)

    def test_index_follows_new_writes(self) -> None:
        self.assertStatus(self.q("升降桌"), Status.NOT_FOUND)
        ingest_document(self.store, make("新文档｜追加\n白色升降桌放在窗边。\n", "SIMSEARCH3"), SCOPE,
                        object_id="doc/new")
        self.assertEqual(self.q("升降桌")["result"]["hits"][0]["object_id"], "doc/new")

    def test_blocked_source_snippet_withheld(self) -> None:
        ref = "src/google-drive/SIMSEARCH1@v1"
        self.conn.execute("UPDATE source_refs SET status='BLOCKED' WHERE source_ref_id=?", (ref,))
        env = self.q("灯光")
        hit = [h for h in env["result"]["hits"] if h["source_ref_id"] == ref][0]
        self.assertIsNone(hit["snippet"])
        self.assertIsNone(hit["anchor"])
        self.assertEqual(hit["snippet_withheld"], "BLOCKED")
        self.assertTrue(any(w.startswith("SNIPPET_WITHHELD") for w in env["warnings"]))

    def test_bad_inputs(self) -> None:
        self.assertStatus(self.q("灯光", "nope:scope"), Status.INVALID_SCOPE)
        self.assertStatus(self.q(""), Status.INVALID_SCOPE)
        self.assertStatus(self.q("灯光", kinds=["bogus"]), Status.INVALID_SCOPE)
        self.assertStatus(dispatch(self.core, "search", {"query": "x", "scope": SCOPE,
                                                         "expand": True}), Status.INVALID_SCOPE)

    def test_current_aspects_are_searchable_in_fixture_corpus(self) -> None:
        env = dispatch(self.core, "search", {"query": "DLC", "scope": "project:DLC-01"})
        self.assertStatus(env, Status.OK)
        self.assertIn("project/DLC-01", {h["object_id"] for h in env["result"]["hits"]})

    def test_resolve_object_falls_back_to_full_text(self) -> None:
        env = dispatch(self.core, "resolve_object", {"query": "蓝色竖线 章节", "scope": SCOPE})
        self.assertStatus(env, Status.OK)
        cands = env["result"]["candidates"]
        self.assertEqual(cands[0]["object_id"], "doc/search")
        self.assertEqual(cands[0]["match_reason"], "full_text")
