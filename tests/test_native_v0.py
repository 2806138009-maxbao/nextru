"""STORAGE x WRITE v0: Nexus-native knowledge documents (acceptance criteria 1-8)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store, connect, init_schema
from nexus.native import NativeDocs, NativeError, backup, restore_backup

SCOPE = "research:native-test"
SPEC = {"title": "Native Test Article", "children": [
    {"kind": "section", "title": "Background", "children": [
        "First paragraph about lumen tracking.",
        {"kind": "section", "title": "Detail", "children": ["Nested paragraph on wavelets."]}]},
    {"kind": "section", "title": "Findings", "children": ["Finding one: cadence matters."]}]}


def fresh_db() -> str:
    d = Path(tempfile_mkdtemp())
    path = str(d / "native.sqlite")
    conn = connect(path)
    init_schema(conn)
    s = Store(conn)
    s.add_scope(SCOPE, "research", "test")
    conn.execute("INSERT INTO objects VALUES ('doc/mirror1','document','Mirrored Drive Doc',?, '2026-11-08')",
                 (SCOPE,))
    conn.execute("INSERT INTO source_refs(source_ref_id, source_system, native_id, revision, access_scope,"
                 " status, archive_path) VALUES ('ref-m1','GOOGLE_DRIVE','m1','v3',?, 'OK','sealed/m1')", (SCOPE,))
    conn.execute("INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle,"
                 " verification, revision, is_current, source_ref_id, updated_at) VALUES"
                 " ('a1','doc/mirror1','content','{}','1','CURRENT','VERIFIED',1,1,'ref-m1','2026-11-08')")
    conn.close()
    return path


def paragraphs(doc: dict) -> dict[str, str]:
    return {n["node_id"]: n["text"] for n in doc["nodes"] if n["kind"] == "paragraph"}


class NativeV0(unittest.TestCase):
    def setUp(self):
        self.path = fresh_db()
        self.conn = connect(self.path)
        init_schema(self.conn)
        self.n = NativeDocs(self.conn)
        self.doc = self.n.create(SCOPE, SPEC)["doc_id"]

    def tearDown(self):
        self.conn.close()

    def first_para(self, rev=None):
        d = self.n.read(self.doc, rev)
        return next(n for n in d["nodes"] if n["kind"] == "paragraph")

    # 1 + 2
    def test_c1_c2_create_tree_and_stable_ids(self):
        d = self.n.read(self.doc)
        kinds = [(n["kind"], n["depth"]) for n in d["nodes"]]
        self.assertEqual(kinds, [("section", 0), ("paragraph", 1), ("section", 1), ("paragraph", 2),
                                 ("section", 0), ("paragraph", 1)])
        self.assertEqual(d["nodes"][3]["section_path"], "Background > Detail")
        ids = [n["node_id"] for n in d["nodes"]]
        p = self.first_para()
        self.n.edit(self.doc, 1, [{"op": "set_text", "node_id": p["node_id"], "body": "changed"}])
        self.assertEqual([n["node_id"] for n in self.n.read(self.doc)["nodes"]], ids)

    # 3
    def test_c3_edit_makes_revision_old_still_readable(self):
        p = self.first_para()
        out = self.n.edit(self.doc, 1, [{"op": "set_text", "node_id": p["node_id"], "body": "Rewritten."}])
        self.assertEqual(out["revision"], 2)
        self.assertEqual(paragraphs(self.n.read(self.doc))[p["node_id"]], "Rewritten.")
        self.assertEqual(paragraphs(self.n.read(self.doc, 1))[p["node_id"]],
                         "First paragraph about lumen tracking.")
        diff = self.n.diff(self.doc, 1, 2)["changes"]
        self.assertEqual([(c["node_id"], c["change"]) for c in diff], [(p["node_id"], "body")])
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE native_tree SET title = 'x' WHERE doc_id = ?", (self.doc,))

    # 4
    def test_c4_stale_write_refused_across_connections(self):
        other = NativeDocs(connect(self.path))
        p = self.first_para()["node_id"]
        self.n.edit(self.doc, 1, [{"op": "set_text", "node_id": p, "body": "A"}])
        with self.assertRaises(NativeError) as e:
            other.edit(self.doc, 1, [{"op": "set_text", "node_id": p, "body": "B"}])
        self.assertEqual(e.exception.code, "VERSION_CONFLICT")
        self.assertEqual(paragraphs(self.n.read(self.doc))[p], "A")

    def test_c4_mirror_and_agent_refused(self):
        with self.assertRaises(NativeError) as e:
            self.n.edit("doc/mirror1", 1, [{"op": "delete", "node_id": "x"}])
        self.assertEqual(e.exception.code, "MIRROR_READ_ONLY")
        with self.assertRaises(NativeError) as e:
            self.n.edit(self.doc, 1, [{"op": "delete", "node_id": self.first_para()["node_id"]}],
                        actor="agent:ds")
        self.assertEqual(e.exception.code, "AUTHORITY_DENIED")
        with self.assertRaises(NativeError) as e:
            self.n.propose_edit("doc/mirror1", 1, [], "agent:ds")
        self.assertEqual(e.exception.code, "MIRROR_READ_ONLY")

    # 5 + 6
    def test_c5_c6_relation_proposal_then_accept(self):
        p = self.first_para()["node_id"]
        with self.assertRaises(NativeError):
            self.n.propose_relation(SCOPE, self.doc, "cites", "doc/mirror1", [], "agent:ds")
        out = self.n.propose_relation(SCOPE, self.doc, "cites", "doc/mirror1",
                                      [{"doc_id": self.doc, "node_id": p}], "agent:ds", "mentions it")
        rel = self.conn.execute("SELECT * FROM relations WHERE relation_id = ?", (out["relation_id"],)).fetchone()
        self.assertEqual(rel["verification"], "UNVERIFIED")
        ref = json.loads(rel["provenance_refs"])[0]
        self.assertEqual(ref, f"nexus:{self.doc}@1#{p}")
        with self.assertRaises(NativeError) as e:
            self.n.accept(out["proposal_id"], actor="agent:ds")
        self.assertEqual(e.exception.code, "AUTHORITY_DENIED")
        self.n.accept(out["proposal_id"])
        rel = self.conn.execute("SELECT verification FROM relations WHERE relation_id = ?",
                                (out["relation_id"],)).fetchone()
        self.assertEqual(rel[0], "VERIFIED")

    def test_c6_edit_proposal_accept_and_stale(self):
        p = self.first_para()["node_id"]
        a = self.n.propose_edit(self.doc, 1, [{"op": "set_text", "node_id": p, "body": "From agent"}], "agent:ds")
        b = self.n.propose_edit(self.doc, 1, [{"op": "insert", "parent_id": None, "after": None,
                                               "kind": "paragraph", "body": "Intro"}], "agent:ds")
        self.assertEqual(paragraphs(self.n.read(self.doc))[p], "First paragraph about lumen tracking.")
        self.assertEqual(self.n.accept(a["proposal_id"])["revision"], 2)
        with self.assertRaises(NativeError) as e:
            self.n.accept(b["proposal_id"])
        self.assertEqual(e.exception.code, "VERSION_CONFLICT")
        states = {x["proposal_id"]: x["status"] for x in self.n.proposals()["proposals"]}
        self.assertEqual(states, {a["proposal_id"]: "ACCEPTED", b["proposal_id"]: "STALE"})
        self.assertEqual(self.n.read(self.doc)["revision_info"]["proposal_id"], a["proposal_id"])

    # 7
    def test_c7_index_updates_and_new_session_reads_latest(self):
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        self.assertTrue(any(h["kind"] == "native" for h in tools.nexus_search("lumen")["hits"]))
        p = self.first_para()["node_id"]
        self.n.edit(self.doc, 1, [{"op": "set_text", "node_id": p, "body": "Now about quasar timing."}])
        fresh = AgentTools(NexusCore(Store(connect(self.path))), SCOPE)   # another session
        self.assertFalse(any(h["kind"] == "native" for h in fresh.nexus_search("lumen")["hits"]))
        hit = next(h for h in fresh.nexus_search("quasar")["hits"] if h["kind"] == "native")
        self.assertEqual(hit["text"], "Now about quasar timing.")
        r = fresh.nexus_read(self.doc)
        self.assertEqual(r["revision"], 2)
        self.assertEqual(r["nodes"][1]["text"], "Now about quasar timing.")
        self.assertEqual(fresh.nexus_read(self.doc, revision=1)["nodes"][1]["text"],
                         "First paragraph about lumen tracking.")

    # 8
    def test_c8_history_restore_and_backup_restore(self):
        p = self.first_para()["node_id"]
        self.n.edit(self.doc, 1, [{"op": "delete", "node_id": p}])
        out = self.n.restore(self.doc, 1, expected_revision=2)
        self.assertEqual(out["revision"], 3)
        h = self.n.history(self.doc)["revisions"]
        self.assertEqual(h[2]["restored_from"], 1)
        self.assertEqual(h[2]["tree_sha256"], h[0]["tree_sha256"])
        d = Path(self.path).parent
        b = backup(self.path, str(d / "bk" / "native.bak.sqlite"))
        self.assertEqual(b["integrity_check"], "ok")
        self.n.edit(self.doc, 3, [{"op": "set_text", "node_id": p, "body": "after backup"}])
        r = restore_backup(str(d / "bk" / "native.bak.sqlite"), str(d / "restored.sqlite"))
        self.assertTrue(r["matches_manifest"])
        self.assertEqual(r["native_heads"][self.doc], 3)
        self.assertEqual(NativeDocs(connect(str(d / "restored.sqlite"))).read(self.doc)["revision"], 3)
        with self.assertRaises(NativeError):
            restore_backup(str(d / "bk" / "native.bak.sqlite"), self.path)

    def test_title_only_words_find_the_document(self):
        """A word that appears only in a native document's title must find it, also when the
        document was created or retitled after the last full index rebuild (incremental path)."""
        from nexus import search as si
        si.ensure_index(self.conn)
        doc = self.n.create(SCOPE, {"title": "Zephyrion progress sync", "children": ["plain body text"]})["doc_id"]
        hits = lambda q: [h for h in AgentTools(NexusCore(Store(connect(self.path))), SCOPE)  # noqa: E731
                          .nexus_search(q)["hits"] if h["object_id"] == doc]
        self.assertTrue(hits("Zephyrion"))
        self.assertEqual(hits("Zephyrion")[0]["kind"], "object")
        self.n.edit(doc, 1, [], title="Quillfeather notes")
        self.assertFalse(hits("Zephyrion"))
        self.assertTrue(hits("Quillfeather"))
        self.n.archive(doc, True)
        self.assertFalse(hits("Quillfeather"))
        self.n.archive(doc, False)
        self.assertTrue(hits("Quillfeather"))
        # the incremental path did not invalidate the full index
        self.assertEqual(self.conn.execute("SELECT value FROM search_meta WHERE key='fingerprint'")
                         .fetchone()[0], si._fingerprint(self.conn))

    def test_writes_never_force_full_index_rebuild(self):
        from nexus import search as si
        si.ensure_index(self.conn)
        p = self.first_para()["node_id"]
        self.n.propose_relation(SCOPE, self.doc, "cites", "doc/mirror1",
                                [{"doc_id": self.doc, "node_id": p}], "agent:ds")
        self.n.propose_edit(self.doc, 1, [{"op": "set_text", "node_id": p, "body": "x"}], "agent:ds",
                            evidence=[{"doc_id": self.doc, "node_id": p}])
        self.n.edit(self.doc, 1, [{"op": "set_text", "node_id": p, "body": "y"}])
        stored = self.conn.execute("SELECT value FROM search_meta WHERE key='fingerprint'").fetchone()[0]
        self.assertEqual(stored, si._fingerprint(self.conn))

    def test_document_proposal_created_only_on_accept(self):
        spec = {"title": "IF draft", "children": [{"kind": "section", "title": "Premise",
                                                   "children": ["An invented storyline."]}]}
        out = self.n.propose_document(SCOPE, spec, "agent:ds")
        self.assertEqual((out["sections"], out["paragraphs"]), (1, 1))
        before = self.conn.execute("SELECT COUNT(*) FROM native_documents").fetchone()[0]
        with self.assertRaises(NativeError):
            self.n.accept(out["proposal_id"], actor="agent:ds")
        with self.assertRaises(NativeError):
            self.n.propose_document(SCOPE, {"title": "", "children": ["x"]}, "agent:ds")
        with self.assertRaises(NativeError):
            self.n.propose_document(SCOPE, {"title": "empty", "children": []}, "agent:ds")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM native_documents").fetchone()[0], before)
        acc = self.n.accept(out["proposal_id"])
        d = self.n.read(acc["doc_id"])
        self.assertEqual(d["title"], "IF draft")
        self.assertEqual(d["revision_info"]["proposal_id"], out["proposal_id"])
        self.assertEqual(d["nodes"][1]["section_path"], "Premise")

    def test_agent_direct_writes_and_undo(self):
        n = NativeDocs(self.conn, agent_writes=True)
        a = "agent:ds"
        doc = n.create(SCOPE, {"title": "Agent note", "children": ["fact one"]}, a)["doc_id"]
        p = n.read(doc)["nodes"][0]["node_id"]
        self.assertEqual(n.edit(doc, 1, [{"op": "set_text", "node_id": p, "body": "fact two"}], a)["revision"], 2)
        with self.assertRaises(NativeError) as e:
            n.edit(doc, 1, [{"op": "set_text", "node_id": p, "body": "stale"}], a)
        self.assertEqual(e.exception.code, "VERSION_CONFLICT")
        with self.assertRaises(NativeError) as e:
            n.edit("doc/mirror1", 1, [{"op": "delete", "node_id": "x"}], a)
        self.assertEqual(e.exception.code, "MIRROR_READ_ONLY")
        # undo the edit
        self.assertEqual(n.restore(doc, 1, 2, a)["revision"], 3)
        self.assertEqual(paragraphs(n.read(doc))[p], "fact one")
        # an agent's relation needs evidence, is written at once but UNVERIFIED (pending proposal)
        with self.assertRaises(NativeError) as e:
            n.add_relation(SCOPE, doc, "about", "doc/mirror1", a)
        self.assertEqual(e.exception.code, "INVALID_REQUEST")
        # (BASED_ON is an ordinary candidate; plain filing would be confirmed by policy - test_review)
        out = n.add_relation(SCOPE, doc, "BASED_ON", "doc/mirror1", a, evidence=[{"doc_id": doc, "node_id": p}])
        rel = out["relation_id"]
        self.assertEqual((out["status"], out["verification"], out["written_by"]), ("OK", "UNVERIFIED", a))
        row = self.conn.execute("SELECT verification FROM relations WHERE relation_id = ?", (rel,)).fetchone()
        self.assertEqual(row[0], "UNVERIFIED")
        prop = self.conn.execute("SELECT status, proposer FROM native_proposals WHERE proposal_id = ?",
                                 (out["proposal_id"],)).fetchone()
        self.assertEqual(tuple(prop), ("PROPOSED", a))
        # visible to readers at once, marked as a candidate
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        edges = tools.nexus_related(doc, include_candidates=True)["edges"]
        self.assertTrue(any(ed["relation_id"] == rel and ed.get("candidate") for ed in edges))
        # an agent cannot confirm it; the owner can
        with self.assertRaises(NativeError) as e:
            n.accept(out["proposal_id"], a)
        self.assertEqual(e.exception.code, "AUTHORITY_DENIED")
        self.assertEqual(self.n.accept(out["proposal_id"])["verification"], "VERIFIED")
        # undo: removing a pending agent relation withdraws its proposal too
        out2 = n.add_relation(SCOPE, doc, "derived_from", "doc/mirror1", a,
                              evidence=[{"doc_id": doc, "node_id": p}])
        n.remove_relation(out2["relation_id"], a)
        self.assertIsNotNone(self.conn.execute("SELECT expired_at FROM relations WHERE relation_id = ?",
                                               (out2["relation_id"],)).fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT status FROM native_proposals WHERE proposal_id = ?",
                                           (out2["proposal_id"],)).fetchone()[0], "WITHDRAWN")
        # the owner's own relations are VERIFIED directly
        own = self.n.add_relation(SCOPE, doc, "part_of", "doc/mirror1", "user")
        self.assertEqual(self.conn.execute("SELECT verification FROM relations WHERE relation_id = ?",
                                           (own["relation_id"],)).fetchone()[0], "VERIFIED")
        self.n.remove_relation(own["relation_id"], "user")
        # undo the create: out of search, still readable, can come back
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        self.assertTrue(any(h["object_id"] == doc for h in tools.nexus_search("fact one")["hits"]))
        n.archive(doc, True, a)
        self.assertFalse(any(h["object_id"] == doc for h in tools.nexus_search("fact one")["hits"]))
        self.assertTrue(n.read(doc)["archived"])
        n.archive(doc, False, a)
        self.assertTrue(any(h["object_id"] == doc for h in tools.nexus_search("fact one")["hits"]))
        kinds = {r[0] for r in self.conn.execute("SELECT event_type FROM events WHERE object_id = ?", (doc,))}
        self.assertEqual(kinds, {"AGENT_WRITE"})
        # without agent_writes the same agent is refused
        with self.assertRaises(NativeError) as e:
            self.n.edit(doc, 3, [{"op": "delete", "node_id": p}], a)
        self.assertEqual(e.exception.code, "AUTHORITY_DENIED")

    def test_mcp_write_tools_and_timing_log(self):
        import asyncio
        from nexus.mcp_server import build_server
        from nexus.native import timing_summary, write_log_path
        names = {t.name for t in asyncio.run(build_server(self.path, SCOPE).list_tools())}
        self.assertNotIn("nexus_create_document", names)
        server = build_server(self.path, SCOPE, agent_name="ds")
        names = {t.name for t in asyncio.run(server.list_tools())}
        self.assertTrue({"nexus_create_document", "nexus_edit_document", "nexus_add_relation",
                         "nexus_restore_revision", "nexus_archive_document",
                         "nexus_remove_relation"} <= names)
        self.assertFalse({n for n in names if "propose" in n})
        r = asyncio.run(server.call_tool("nexus_create_document",
                                         {"title": "Via MCP", "children": ["hello"]}))
        blocks = r[0] if isinstance(r, tuple) else r
        blocks = getattr(blocks, "content", blocks)
        out = json.loads("".join(getattr(b, "text", "") for b in blocks))
        self.assertEqual((out["status"], out["revision"]), ("OK", 1))
        self.assertEqual(self.n.read(out["doc_id"])["revision_info"]["author"], "agent:ds")
        summary = timing_summary(self.path)
        self.assertEqual(set(summary["by_tool"]), {"nexus_create_document"})
        write_log_path(self.path).unlink()

    def test_http_is_loopback_only(self):
        from nexus.mcp_server import is_loopback, main
        self.assertTrue(all(map(is_loopback, ["127.0.0.1", "::1", "localhost", "127.0.0.2"])))
        self.assertFalse(any(map(is_loopback, ["0.0.0.0", "::", "10.0.0.245", "203.0.113.10", "example.com"])))
        with self.assertRaises(SystemExit):
            main(["--db", self.path, "--scope", SCOPE, "--transport", "http", "--host", "0.0.0.0"])

    def test_http_health_route(self):
        from starlette.testclient import TestClient
        from nexus.mcp_server import build_server
        app = build_server(self.path, SCOPE, agent_name="ds").streamable_http_app()
        body = TestClient(app, base_url="http://127.0.0.1").get("/health").json()
        self.assertEqual((body["status"], body["integrity"], body["native_documents"], body["writes_enabled"]),
                         ("OK", "ok", 1, True))

    def test_health_ok_after_another_process_writes(self):
        """FTS5 caches its index structure per connection; after another connection writes,
        PRAGMA quick_check on the long-lived server connection reports a false 'malformed
        inverted index' / checksum mismatch. /health must check on a fresh connection."""
        from starlette.testclient import TestClient
        from nexus.mcp_server import build_server
        server = build_server(self.path, SCOPE, agent_name="ds")
        client = TestClient(server.streamable_http_app(), base_url="http://127.0.0.1")
        AgentTools(NexusCore(Store(self.conn)), SCOPE)  # make sure the index exists
        import asyncio
        asyncio.run(server.call_tool("nexus_search", {"query": "lumen"}))   # server conn caches FTS
        self.assertEqual(client.get("/health").json()["integrity"], "ok")
        other = NativeDocs(connect(self.path))                              # "another process"
        for i in range(30):
            other.create(SCOPE, {"title": f"note {i}", "children": [f"lumen body {i} " * 20]})
        body = client.get("/health").json()
        self.assertEqual(body["integrity"], "ok")
        self.assertEqual(body["native_documents"], 31)

    def test_export_round_trip_has_ids(self):
        md = self.n.export_markdown(self.doc)
        self.assertIn(f"nexus doc={self.doc} revision=1", md)
        self.assertIn("## Background <!-- node=", md)
        self.assertEqual(len(self.n.export_json(self.doc)["revisions"]), 1)


if __name__ == "__main__":
    unittest.main()
