"""Self-declared document status: a separate aspect, verbatim declarations, conservative currency."""

from __future__ import annotations

import json
import unittest

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store, connect, init_schema
from nexus.ingest.doc_status import declarations, import_doc_status
from nexus import search as si
from test_native_v0 import SCOPE, fresh_db

NEW = "NEWMISSIONNEWMIS_full"
DOCS = {
    "doc/WITHDRAWNWITHDRA": ("Repository Governance Doctrine v0.1｜草案", ["Doctrine", "状态：WITHDRAWN / HISTORICAL PROPOSAL / NOT ADOPTED / NO AUTHORITY"]),
    "doc/HISTORYHISTORYHI": ("HISTORY｜DPP2 Scenario Weeks｜Superseded by DLC-01",
                             ["HISTORY", f"本文件不再维护 Current；有效 DLC Mission：https://docs.google.com/document/d/{NEW}/edit ；正式 Owner：https://docs.google.com/document/d/SPINESPINESPINE0_full/edit 。"]),
    "doc/NEWMISSIONNEWMIS": ("MISSION｜DLC-01", ["MISSION", "mission_id: X | object_type: MISSION | mission_state: ACTIVE | domain: EXU"]),
    "doc/SPINESPINESPINE0": ("01｜STANDARD｜Global Semantic Spine v0.6", ["Spine", "Status", "ADOPTED / CROSS-DOMAIN SEMANTIC STANDARD / v0.6"]),
    "doc/PLAINPLAINPLAIN0": ("Lecture notes week 3", ["Some notes", "nothing declared here"]),
    "doc/TITLEHISTTITLEHI": ("90｜HISTORICAL｜To B Roadmap｜Retired", ["Roadmap", "Status", "ACTIVE / ROADMAP"]),
}


class DocStatus(unittest.TestCase):
    def setUp(self):
        self.path = fresh_db()
        self.conn = connect(self.path)
        init_schema(self.conn)
        for oid, (name, paras) in DOCS.items():
            self.conn.execute("INSERT INTO objects VALUES (?, 'document', ?, ?, '2026-11-10')", (oid, name, SCOPE))
            ref = f"ref-{oid[4:]}"
            self.conn.execute("INSERT INTO source_refs(source_ref_id, source_system, native_id, revision, access_scope,"
                              " status) VALUES (?, 'GOOGLE_DRIVE', ?, 'v1', ?, 'OK')", (ref, oid[4:], SCOPE))
            for i, t in enumerate(paras):
                self.conn.execute(
                    "INSERT INTO observations(observation_id, object_id, dimension, statement, stance, detail,"
                    " source_ref_ids, created_at) VALUES (?, ?, 'document_paragraph', ?, 'CASE', ?, ?, '2026-11-10')",
                    (f"obs/{oid}@v1/{i:04d}", oid, t, json.dumps({"anchor": f"h0.{i + 1}-abc{i}"}), json.dumps([ref])))
        self.conn.commit()
        si.ensure_index(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def status(self, oid):
        r = self.conn.execute("SELECT payload FROM aspects WHERE object_id = ? AND name = 'doc_status' AND is_current = 1",
                              (oid,)).fetchone()
        return json.loads(r[0]) if r else None

    def test_currency_rules(self):
        out = import_doc_status(self.conn, SCOPE)
        self.assertEqual(out["errors"], [])
        got = {oid: self.status(oid)["currency"] for oid in DOCS}
        self.assertEqual(got, {"doc/WITHDRAWNWITHDRA": "WITHDRAWN", "doc/HISTORYHISTORYHI": "HISTORICAL",
                               "doc/NEWMISSIONNEWMIS": "CURRENT", "doc/SPINESPINESPINE0": "CURRENT",
                               "doc/PLAINPLAINPLAIN0": "UNDECLARED",
                               # header says ACTIVE, the title was relabelled HISTORICAL: the retired state wins
                               "doc/TITLEHISTTITLEHI": "HISTORICAL"})
        m = self.status("doc/NEWMISSIONNEWMIS")
        self.assertEqual((m["object_type"], m["mission_state"]), ("MISSION", "ACTIVE"))
        self.assertTrue(any(d["anchor"] and "ADOPTED" in d["text"] for d in self.status("doc/SPINESPINESPINE0")["declared"]))
        # the imported content aspect is untouched (still never Current by itself)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM aspects WHERE name = 'content'").fetchone()[0], 1)

    def test_self_declared_supersession_and_reruns(self):
        out = import_doc_status(self.conn, SCOPE)
        self.assertEqual(out["supersedes"], 1)
        rel = self.conn.execute("SELECT from_id, to_id, verification FROM relations WHERE relation_type = 'SUPERSEDES'").fetchall()
        self.assertEqual([tuple(r) for r in rel], [("doc/NEWMISSIONNEWMIS", "doc/HISTORYHISTORYHI", "VERIFIED")])
        self.assertEqual(self.status("doc/HISTORYHISTORYHI")["superseded_by"], "doc/NEWMISSIONNEWMIS")
        again = import_doc_status(self.conn, SCOPE)
        self.assertEqual((again["written"], again["supersedes"]), (0, 0))
        # the index was kept in step: no full rebuild pending, status searchable
        self.assertEqual(self.conn.execute("SELECT value FROM search_meta WHERE key='fingerprint'").fetchone()[0],
                         si._fingerprint(self.conn))
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        r = tools.nexus_read("doc/WITHDRAWNWITHDRA")
        self.assertEqual(r["status_declared"]["currency"], "WITHDRAWN")

    def test_declarations_never_guess(self):
        d = declarations("Plain title", [("h0.1-x", "just text")])
        self.assertEqual((d["currency"], d["declared"]), ("UNDECLARED", []))
        # negated current-words do not make a document Current
        for txt in ("TEMPORARY / NON-AUTHORITATIVE / HOLDING ONLY", "CALIBRATION REFERENCE / NO CURRENT INSTRUCTION"):
            self.assertEqual(declarations("x", [("h0.1-a", "Status"), ("h0.2-b", txt)])["currency"], "UNDECLARED", txt)
        self.assertEqual(declarations("x", [("h0.1-a", "状态：HISTORICAL ENGINEERING EVIDENCE / NOT ACTIVE PROGRAM")])["currency"],
                         "HISTORICAL")


if __name__ == "__main__":
    unittest.main()
