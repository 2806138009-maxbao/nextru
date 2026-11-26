"""Every MCP tool call is timed into <db>.calls.jsonl (nexus.calls)."""

from __future__ import annotations

import asyncio
import json

from helpers import NexusTest

from nexus import calls
from nexus.ingest.register import import_register
from nexus.mcp_server import build_server
from nexus.search import ensure_index
from test_register_import import SCOPE, export


class CallsLog(NexusTest):

    def setUp(self):
        super().setUp()
        import_register(self.store, export("1"), SCOPE, "T100")
        ensure_index(self.conn)
        self.conn.commit()

    def records(self):
        p = calls.log_path(self.db_path)
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_reads_and_writes_are_logged_with_timing_and_short_args(self):
        for fmt in ("full", "compact"):
            server = build_server(self.db_path, SCOPE, result_format=fmt, agent_name="tester")
            asyncio.run(server.call_tool("nexus_search", {"query": "Pulse shaping " + "x" * 100}))
            asyncio.run(server.call_tool("nexus_learning_context", {"topic": "shape"}))
            asyncio.run(server.call_tool("nexus_create_document", {
                "title": "T", "children": [{"kind": "paragraph", "body": "secret body text " * 20}]}))
        recs = self.records()
        self.assertEqual([r["tool"] for r in recs], ["nexus_search", "nexus_learning_context",
                                                     "nexus_create_document"] * 2)
        for r in recs:
            self.assertEqual(r["actor"], "agent:tester")
            self.assertGreaterEqual(r["elapsed_ms"], 0)
            self.assertGreater(r["result_chars"], 0)
        self.assertTrue(recs[0]["args"]["query"].endswith("…"))
        self.assertLessEqual(len(recs[0]["args"]["query"]), 61)
        create = recs[2]["args"]
        self.assertEqual(create["children"], "<1>")
        self.assertNotIn("secret body text", json.dumps(recs, ensure_ascii=False))
        self.assertEqual(recs[1]["status"], "OK")
        self.assertEqual(recs[5]["status"], "OK")

    def test_read_only_server_logs_as_reader_and_report_groups_episodes(self):
        server = build_server(self.db_path, SCOPE)
        asyncio.run(server.call_tool("nexus_resolve", {"target": "DEMO0020"}))
        recs = self.records()
        self.assertEqual((recs[0]["tool"], recs[0]["actor"]), ("nexus_resolve", "reader"))
        fake = [dict(recs[0], at="2026-11-10T17:00:00.000Z"), dict(recs[0], at="2026-11-10T17:00:40.000Z"),
                dict(recs[0], at="2026-11-10T17:10:00.000Z")]
        eps = calls.episodes(fake, 180)
        self.assertEqual([len(e) for e in eps], [2, 1])
        self.assertIn("longest pause 40s", calls.report(fake, 180))
