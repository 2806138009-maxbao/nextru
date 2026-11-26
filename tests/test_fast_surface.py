"""Fewer agent round trips: identifier spellings, document summaries in search, source freshness in read.

Synthetic documents only; the live check uses fake callables.
"""

from __future__ import annotations

import json

from helpers import NexusTest

from nexus.agent_tools import AgentTools
from nexus.freshness import snapshot_info
from nexus.ingest import FetchedDocument, ingest_document
from nexus.search import identifier_variants, query_groups

SCOPE = "research:fast"

MISSION = """MISSION｜SIM-DLC01｜Winter Return
状态行｜ACTIVE / WAITING_FOR_BRIEF / VERIFIED 2026-10-28
第二段：已通知老师，等待正式分组。
一｜细节
DLC-01 的行政线已经处理完。
"""
OLD = """HISTORY｜旧研究｜Superseded by DLC-01
旧的推演，不再是现行结论。
DLC-01 之前的方案。DLC01 曾经讨论过。DLC1 也出现过。
"""
OTHER = """课程｜DEMO0009 入口
实验安排在周三。
"""


def doc(text: str, did: str, version: str = "3") -> FetchedDocument:
    return FetchedDocument("GOOGLE_DRIVE", did, text.splitlines()[0], version,
                           "2026-11-04T18:28:06Z", None, text)


class QueryNormalisationTests(NexusTest):

    def test_identifier_variants(self) -> None:
        self.assertEqual(identifier_variants("dlc1"), ["dlc1", "dlc-1", "dlc01", "dlc-01"])
        self.assertIn("DLC1", identifier_variants("DLC01"))
        self.assertEqual(identifier_variants("现在"), ["现在"])
        self.assertEqual(identifier_variants("report"), ["report"])

    def test_mixed_and_hyphenated_queries(self) -> None:
        g = query_groups("dlc1现在是什么进度")
        self.assertEqual(g[0][0], "dlc1")
        self.assertIn(["现在"], g)
        self.assertEqual(query_groups("DLC-01 现在怎么样了")[0][0], "DLC01")
        self.assertEqual(query_groups("DEMO_0008")[0][0], "DEMO0008")


class FastSurfaceTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        for text, did, oid in ((MISSION, "SIMMISSION", "doc/mission"), (OLD, "SIMOLD", "doc/old"),
                               (OTHER, "SIMOTHER", "doc/other")):
            ingest_document(self.store, doc(text, did), SCOPE, object_id=oid)
        self.tools = AgentTools(self.core, SCOPE)

    def test_short_code_finds_all_spellings_and_current_doc_first(self) -> None:
        out = self.tools.nexus_search("dlc1现在什么进度")
        self.assertEqual(out["status"], "OK")
        ids = [d["object_id"] for d in out["documents"]]
        self.assertEqual(ids[0], "doc/mission")      # title match, not labelled historical
        self.assertIn("doc/old", ids)                 # still shown, ranked after
        head = out["documents"][0]["head"]
        self.assertTrue(head[0]["text"].startswith("状态行"))  # opening paragraphs in document order
        self.assertEqual(out["documents"][0]["source"]["snapshot_version"], "3")
        texts = [h for h in out["hits"] if "text" in h]
        self.assertTrue(texts and all(h["kind"] == "paragraph" for h in texts))
        self.assertLessEqual(len(texts), 4)

    def test_history_query_puts_history_first_and_lists_in_document_matches(self) -> None:
        out = self.tools.nexus_search("DLC-01 之前的旧研究")
        self.assertEqual(out["documents"][0]["object_id"], "doc/old")
        current = self.tools.nexus_search("DLC-01 细节")
        top = current["documents"][0]
        self.assertEqual(top["object_id"], "doc/mission")
        shown = {h["anchor"] for h in current["hits"]}
        self.assertTrue(all(m["anchor"] not in shown for m in top.get("also_matching", [])))

    def test_multi_query_one_call(self) -> None:
        out = self.tools.nexus_search(queries=["DLC-01", "DEMO0009", "zzzz不存在"])
        self.assertEqual((out["status"], out["mode"]), ("OK", "multi"))
        self.assertEqual([r["query"] for r in out["results"]], ["DLC-01", "DEMO0009", "zzzz不存在"])
        self.assertEqual(out["results"][1]["documents"][0]["object_id"], "doc/other")
        self.assertEqual(out["results"][2]["status"], "NOT_FOUND")
        for r in out["results"]:
            self.assertLessEqual(len(r["hits"]), 4)
            self.assertLessEqual(sum(1 for h in r["hits"] if "text" in h), 2)
        single = self.tools.nexus_search("DLC-01")            # single-query form unchanged
        self.assertNotIn("mode", single)
        both = self.tools.nexus_search("DLC-01", queries=["DEMO0009"])
        self.assertEqual([r["query"] for r in both["results"]], ["DLC-01", "DEMO0009"])

    def test_read_reports_snapshot_and_live_state(self) -> None:
        r = self.tools.nexus_read("doc/mission")
        self.assertEqual(r["source"]["snapshot_version"], "3")
        self.assertEqual(r["source"]["source_modified"], "2026-11-04T18:28:06Z")
        self.assertEqual(r["source"]["live"]["status"], "NOT_CHECKED")
        same = AgentTools(self.core, SCOPE, live_check=lambda d: {"version": "3", "modifiedTime": "t"})
        self.assertEqual(same.nexus_read("doc/mission")["source"]["live"]["status"], "SAME")
        newer = AgentTools(self.core, SCOPE, live_check=lambda d: {"version": "5", "modifiedTime": "t2"})
        live = newer.nexus_read("doc/mission")["source"]["live"]
        self.assertEqual((live["status"], live["source_version"]), ("CHANGED", "5"))

        def boom(_d):
            raise OSError("offline")
        down = AgentTools(self.core, SCOPE, live_check=boom)
        self.assertEqual(down.nexus_read("doc/mission")["source"]["live"]["status"], "UNAVAILABLE")

    def test_read_says_when_it_is_complete(self) -> None:
        full = self.tools.nexus_read("doc/mission")
        self.assertTrue(full["complete"])
        self.assertIn("end of document", full["returned"])
        part = self.tools.nexus_read("doc/mission", max_chars=10)
        self.assertEqual((part["status"], part["complete"]), ("PARTIAL", False))
        rest = self.tools.nexus_read("doc/mission", offset=part["next_offset"])
        self.assertTrue(rest["complete"])
        self.assertIn(f"paragraphs {part['next_offset'] + 1}-", rest["returned"])

    def test_snapshot_info_tracks_latest_revision(self) -> None:
        ingest_document(self.store, doc(MISSION + "补充。\n", "SIMMISSION", version="4"), SCOPE,
                        object_id="doc/mission")
        self.assertEqual(snapshot_info(self.store, "doc/mission")["snapshot_version"], "4")
        self.assertIsNone(snapshot_info(self.store, "doc/nonexistent"))

    def test_no_local_paths_in_new_fields(self) -> None:
        blob = json.dumps([self.tools.nexus_search("DLC-01"), self.tools.nexus_read("doc/mission")],
                          ensure_ascii=False)
        self.assertNotIn(":\\", blob)
        self.assertNotIn("archive_path", blob)
