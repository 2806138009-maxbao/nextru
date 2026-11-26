"""Entity layer: typed objects with stable ids, aliases, quoted evidence, owner confirmation."""

from __future__ import annotations

import json
import unittest

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus import search as si
from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store, connect, init_schema
from nexus.native import NativeDocs, NativeError
from test_native_v0 import SCOPE, fresh_db

PARA = "Principle: real contribution first; low-friction execution."


class Entities(unittest.TestCase):
    def setUp(self):
        self.path = fresh_db()
        self.conn = connect(self.path)
        init_schema(self.conn)
        # one imported paragraph of the mirrored document (as the Drive importer stores it)
        self.conn.execute(
            "INSERT INTO observations(observation_id, object_id, dimension, statement, stance, detail,"
            " source_ref_ids, created_at) VALUES ('obs/doc/mirror1@v3/0003','doc/mirror1','document_paragraph',?,"
            " 'CASE', ?, '[\"ref-m1\"]', '2026-11-08')",
            (PARA, json.dumps({"anchor": "h0.3-abcd1234", "legacy_anchor": "p003"})))
        self.conn.commit()
        si.ensure_index(self.conn)
        self.conn.commit()
        self.agent = NativeDocs(self.conn, agent_writes=True)
        self.owner = NativeDocs(self.conn)
        self.ev = [{"object_id": "doc/mirror1", "anchor": "h0.3-abcd1234"}]

    def tearDown(self):
        self.conn.close()

    def fresh_tools(self) -> AgentTools:   # another session
        return AgentTools(NexusCore(Store(connect(self.path))), SCOPE)

    def test_agent_entity_needs_evidence_and_a_proper_type(self):
        with self.assertRaises(NativeError) as e:
            self.agent.create_object(SCOPE, "principle", "Real contribution first", "agent:ds")
        self.assertEqual(e.exception.code, "INVALID_REQUEST")
        for bad in ("document", "native_document", "Not A Type", ""):
            with self.assertRaises(NativeError):
                self.agent.create_object(SCOPE, bad, "x", "agent:ds", evidence=self.ev)
        with self.assertRaises(NativeError) as e:
            self.agent.create_object(SCOPE, "principle", "x", "agent:ds",
                                     evidence=[{"object_id": "doc/mirror1", "anchor": "h9.9-nothere"}])
        self.assertEqual(e.exception.code, "INVALID_REQUEST")
        with self.assertRaises(NativeError) as e:
            NativeDocs(self.conn).create_object(SCOPE, "principle", "x", "agent:ds", evidence=self.ev)
        self.assertEqual(e.exception.code, "AUTHORITY_DENIED")

    def test_agent_entity_is_unverified_searchable_and_quoted(self):
        out = self.agent.create_object(SCOPE, "principle", "Real contribution first", "agent:ds",
                                       aliases=["真实贡献优先"], description="Mission philosophy lock",
                                       evidence=self.ev)
        oid = out["object_id"]
        self.assertTrue(oid.startswith("ent/principle/"))
        self.assertEqual((out["verification"], out["status"]), ("UNVERIFIED", "OK"))
        prop = self.conn.execute("SELECT kind, status, proposer FROM native_proposals WHERE proposal_id = ?",
                                 (out["proposal_id"],)).fetchone()
        self.assertEqual(tuple(prop), ("OBJECT", "PROPOSED", "agent:ds"))
        tools = self.fresh_tools()
        for q in ("真实贡献优先", "Real contribution"):
            self.assertTrue(any(h["object_id"] == oid for h in tools.nexus_search(q)["hits"]), q)
        r = tools.nexus_read(oid)
        self.assertEqual(r["status"], "OK")
        fact = next(f for f in r["current_facts"] if f["aspect"] == "entity")
        self.assertEqual(fact["verification"], "UNVERIFIED")
        self.assertTrue(any(o["statement"] == PARA for o in r["observations"]))
        self.assertEqual(r["edges"], [])
        # the full index was not invalidated by the incremental write
        self.assertEqual(self.conn.execute("SELECT value FROM search_meta WHERE key='fingerprint'")
                         .fetchone()[0], si._fingerprint(self.conn))

    def test_relation_to_entity_keeps_the_quote_and_shows_in_read(self):
        oid = self.agent.create_object(SCOPE, "principle", "Real contribution first", "agent:ds",
                                       evidence=self.ev)["object_id"]
        rel = self.agent.add_relation(SCOPE, oid, "STATED_IN", "doc/mirror1", "agent:ds", evidence=self.ev)
        self.assertEqual(rel["verification"], "UNVERIFIED")
        tools = self.fresh_tools()
        edge = next(e for e in tools.nexus_related(oid)["edges"] if e["relation_id"] == rel["relation_id"])
        self.assertTrue(edge["candidate"])
        self.assertEqual(json.dumps(edge, ensure_ascii=False).count(PARA), 1)
        r = tools.nexus_read(oid)
        self.assertEqual([(e["type"], e["direction"], e["other"], e["verification"]) for e in r["edges"]],
                         [("STATED_IN", "out", "doc/mirror1", "UNVERIFIED")])
        # a renamed (or moved) source keeps the link: ids, not file names, carry the relation
        self.conn.execute("UPDATE objects SET display_name = 'Renamed Drive Doc' WHERE object_id = 'doc/mirror1'")
        self.conn.commit()
        r = self.fresh_tools().nexus_read(oid)
        self.assertEqual(r["edges"][0]["other_name"], "Renamed Drive Doc")

    def test_owner_confirms_or_rejects(self):
        a = self.agent.create_object(SCOPE, "decision", "Return on 12-08", "agent:ds", evidence=self.ev)
        b = self.agent.create_object(SCOPE, "decision", "Zephyr wrong entity", "agent:ds", evidence=self.ev)
        with self.assertRaises(NativeError) as e:
            self.agent.accept(a["proposal_id"], "agent:ds")
        self.assertEqual(e.exception.code, "AUTHORITY_DENIED")
        self.assertEqual(self.owner.accept(a["proposal_id"])["verification"], "VERIFIED")
        self.owner.reject(b["proposal_id"], reason="not in the source")
        rows = dict(self.conn.execute("SELECT object_id, verification FROM aspects WHERE name = 'entity'"
                                      " AND is_current = 1").fetchall())
        self.assertEqual((rows[a["object_id"]], rows[b["object_id"]]), ("VERIFIED", "DISPUTED"))
        self.assertFalse(any(h["object_id"] == b["object_id"]
                             for h in self.fresh_tools().nexus_search("Zephyr")["hits"]))
        # the owner's own entity needs no evidence and is VERIFIED at once
        own = self.owner.create_object(SCOPE, "person", "Dr Example", "user", aliases=["Example"])
        self.assertEqual(own["verification"], "VERIFIED")
        self.assertNotIn("proposal_id", own)

    def test_full_rebuild_indexes_aliases_too(self):
        oid = self.agent.create_object(SCOPE, "path", "B1 genuine team cooperation", "agent:ds",
                                       aliases=["Quillpath"], evidence=self.ev)["object_id"]
        self.conn.commit()
        si.rebuild(self.conn)
        self.conn.commit()
        self.assertTrue(any(h["object_id"] == oid for h in self.fresh_tools().nexus_search("Quillpath")["hits"]))


if __name__ == "__main__":
    unittest.main()
