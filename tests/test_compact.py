"""compact-1 presentation: lossless round trip for nexus_read / nexus_search. Synthetic documents only."""

from __future__ import annotations

import json

from helpers import NexusTest

from nexus.agent_tools import AgentTools
from nexus.compact import compact_read, compact_search, drop_redundant_snippets, expand_read, expand_search
from nexus.ingest import FetchedDocument, ingest_document

SCOPE = "research:compact"


def doc(native: str, title: str, n: int, word: str) -> FetchedDocument:
    lines = [title] + [f"{i:03d}｜{word}记录，第{i}条。" + "伦敦生活。" * (i % 7) for i in range(1, n)]
    return FetchedDocument("GOOGLE_DRIVE", native, title, "3", "2026-11-04T18:28:06Z", None, "\n".join(lines) + "\n")


class CompactTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        ingest_document(self.store, doc("SIMA", "A｜Journal", 300, "鲸鱼"), SCOPE, object_id="doc/a")
        ingest_document(self.store, doc("SIMB", "B｜Notes", 40, "鲸鱼和海豹"), SCOPE, object_id="doc/b")
        self.tools = AgentTools(self.core, SCOPE)

    def _read_roundtrip(self, out: dict) -> dict:
        c = compact_read(out)
        self.assertEqual(expand_read(json.loads(json.dumps(c, ensure_ascii=False))), out)
        return c

    def test_read_pages_and_anchors_round_trip(self) -> None:
        first = self.tools.nexus_read("doc/a", max_chars=3000)
        c = self._read_roundtrip(first)
        self.assertEqual(c["status"], "PARTIAL")
        self.assertEqual(c["next_offset"], first["next_offset"])
        self.assertTrue(all(len(r) == 2 for r in c["paragraphs"]))
        self.assertIn("@", c["paragraph_source"])
        self._read_roundtrip(self.tools.nexus_read("doc/a", offset=first["next_offset"], max_chars=3000))
        self._read_roundtrip(self.tools.nexus_read("doc/a", anchors=["h0.5", "h0.299", "h0.7-deadbeef"]))
        self._read_roundtrip(self.tools.nexus_read("doc/a", offset=5000))
        self._read_roundtrip(self.tools.nexus_read("doc/missing"))

    def test_paragraph_with_other_source_keeps_it(self) -> None:
        out = self.tools.nexus_read("doc/b")
        out["paragraphs"][3] = dict(out["paragraphs"][3], source="src/other@v1", lifecycle="SUPERSEDED")
        c = self._read_roundtrip(out)
        self.assertEqual(c["paragraphs"][3][2:], ["src/other@v1", "SUPERSEDED"])
        self.assertEqual(len(c["paragraphs"][2]), 2)

    def test_search_single_and_multi_round_trip(self) -> None:
        for out in (self.tools.nexus_search("鲸鱼"), self.tools.nexus_search("海豹 记录"),
                    self.tools.nexus_search(queries=["鲸鱼", "海豹", "鲸鱼 第12条"]),
                    self.tools.nexus_search("不存在的词语"), self.tools.nexus_search("")):
            c = compact_search(out)
            self.assertEqual(expand_search(json.loads(json.dumps(c, ensure_ascii=False))),
                             drop_redundant_snippets(out))
            self.assertLess(len(json.dumps(c, ensure_ascii=False)), len(json.dumps(out, ensure_ascii=False)) + 30)

    def test_repeated_document_in_multi_lists_only_differences(self) -> None:
        out = self.tools.nexus_search(queries=["鲸鱼", "伦敦生活"])
        c = compact_search(out)
        ids = [[d["object_id"] for d in r["documents"]] for r in c["results"]]
        repeated = set(ids[0]) & set(ids[1])
        self.assertTrue(repeated)
        later = [d for d in c["results"][1]["documents"] if d["object_id"] in repeated]
        self.assertTrue(all("name" not in d and "shown_above" in d for d in later))
