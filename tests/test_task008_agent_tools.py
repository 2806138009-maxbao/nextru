"""Task 008 S4 — four agent tools (and their MCP registration) over dispatch."""

from __future__ import annotations

import asyncio
import json
import os

from helpers import NexusTest

from nexus.agent_tools import INSTRUCTIONS, TOOL_DESCRIPTIONS, AgentTools
from nexus.ingest import FetchedDocument, import_candidates, ingest_document, split_sections

SCOPE = "research:t008t"
DOC = "工具测试｜文档\n一｜器材\n作者 @小黑 先用手机，再买灯光。\n" + "".join(
    f"第{i}段，固定机位练习。\n" for i in range(1, 40))
REF = "src/google-drive/SIMTOOLS@v1"


class AgentToolTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        ingest_document(self.store, FetchedDocument("GOOGLE_DRIVE", "SIMTOOLS", "工具测试", "1",
                                                    None, None, DOC), SCOPE, object_id="doc/tools")
        self.anchor = next(s.anchor for s in split_sections(DOC)[1] if "灯光" in s.body)
        self.legacy = next(s.legacy for s in split_sections(DOC)[1] if "灯光" in s.body)
        import_candidates(self.store, {"run_id": "t", "entities": [
            {"key": "xh", "name": "小黑", "type": "creator",
             "mentions": [{"source_ref_id": REF, "anchor": self.anchor, "quote": "作者 @小黑"}]},
            {"key": "light", "name": "灯光", "type": "concept",
             "mentions": [{"source_ref_id": REF, "anchor": self.anchor, "quote": "再买灯光"}]}],
            "relations": [{"from": "xh", "to": "light", "type": "PRIORITISES",
                           "source_ref_id": REF, "anchor": self.anchor,
                           "quote": "先用手机，再买灯光", "note": "顺序"}]}, SCOPE)
        self.tools = AgentTools(self.core, SCOPE)

    def test_empty_search_is_catalog(self) -> None:
        out = self.tools.nexus_search("")
        self.assertEqual(out["mode"], "catalog")
        ids = {o["object_id"] for o in out["objects"]}
        self.assertTrue({"doc/tools", "creator/小黑", "concept/灯光"} <= ids)
        self.assertEqual(out["relation_types"][0]["type"], "PRIORITISES")

    def test_search_then_read_by_anchor(self) -> None:
        hit = next(h for h in self.tools.nexus_search("灯光")["hits"] if h["kind"] == "paragraph")
        out = self.tools.nexus_read(hit["object_id"], anchors=[hit["anchor"], self.legacy, "zzz"])
        self.assertEqual([p["anchor"] for p in out["paragraphs"]], [self.anchor])
        self.assertEqual(out["anchors_not_found"], ["zzz"])

    def test_read_pages_long_documents(self) -> None:
        first = self.tools.nexus_read("doc/tools", max_chars=400)
        self.assertEqual(first["status"], "PARTIAL")
        second = self.tools.nexus_read("doc/tools", offset=first["next_offset"], max_chars=100000)
        self.assertEqual(second["status"], "OK")
        self.assertEqual(len(first["paragraphs"]) + len(second["paragraphs"]),
                         first["paragraphs_total"])

    def test_related_carries_quote_and_candidate_flag(self) -> None:
        out = self.tools.nexus_related("creator/小黑")
        edge = out["edges"][0]
        self.assertTrue(edge["candidate"])
        self.assertEqual(edge["verification"], "UNVERIFIED")
        self.assertEqual(edge["evidence"][0]["quote"], "先用手机，再买灯光")
        self.assertEqual(edge["evidence"][0]["anchor"], self.anchor)
        strict = self.tools.nexus_related("creator/小黑", include_candidates=False)
        self.assertEqual(strict["edges"], [])
        self.assertEqual(strict["excluded_candidates"], 1)

    def test_scope_is_enforced(self) -> None:
        other = AgentTools(self.core, "project:DLC-01")
        self.assertEqual(other.nexus_read("doc/tools")["status"], "NOT_FOUND")
        self.assertNotIn("doc/tools", {h["object_id"] for h in other.nexus_search("灯光")["hits"]})
        bad = AgentTools(self.core, "no:such-scope")
        self.assertEqual(bad.nexus_search("灯光")["status"], "INVALID_SCOPE")

    def test_trace_relation(self) -> None:
        rel = self.tools.nexus_related("creator/小黑")["edges"][0]["relation_id"]
        out = self.tools.nexus_trace(rel)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["item"]["kind"], "relation")

    def test_no_local_paths_leave(self) -> None:
        blob = json.dumps([self.tools.nexus_search(""), self.tools.nexus_read("doc/tools"),
                           self.tools.nexus_related("creator/小黑")], ensure_ascii=False)
        self.assertNotIn(os.path.dirname(self.db_path), blob)
        self.assertNotIn("C:\\Users", blob)

    def test_instructions_explain_document_reading(self) -> None:
        self.assertIn("Documents and ASR transcripts are read", INSTRUCTIONS)
        # Nexus is described as a working, writable knowledge base, not a read-only store
        self.assertNotIn("read-only knowledge store", INSTRUCTIONS)
        self.assertIn("Nexus-native documents", INSTRUCTIONS)
        # the write / undo tools are registered only when the server is started with --agent-name
        self.assertEqual(set(TOOL_DESCRIPTIONS), {"nexus_search", "nexus_read",
                                                  "nexus_related", "nexus_trace",
                                                  "nexus_create_document", "nexus_edit_document",
                                                  "nexus_add_relation", "nexus_restore_revision",
                                                  "nexus_archive_document", "nexus_remove_relation",
                                                  "nexus_create_object",
                                                  "nexus_learning_context", "nexus_resolve",
                                                  "nexus_register_add_row", "nexus_register_edit"})


class McpRegistrationTests(NexusTest):

    def test_server_lists_four_tools_and_calls_through(self) -> None:
        try:
            from nexus.mcp_server import build_server
        except ImportError as exc:  # pragma: no cover - mcp not installed
            self.skipTest(f"mcp not installed: {exc}")
        server = build_server(self.db_path, "project:DLC-01")
        names = sorted(t.name for t in asyncio.run(server.list_tools()))
        self.assertEqual(names, ["nexus_file_info", "nexus_file_view", "nexus_learning_context", "nexus_read", "nexus_related", "nexus_resolve",
                                 "nexus_search", "nexus_trace"])
        result = asyncio.run(server.call_tool("nexus_search", {"query": "DLC"}))
        text = json.dumps(result, ensure_ascii=False, default=str)
        self.assertIn("project/DLC-01", text)
