"""A course register moved from its Sheet into Nexus: cutover, cell edits, new rows, Obsidian notes + Bases."""

from __future__ import annotations

import os
import time
from pathlib import Path

from helpers import NexusTest, tempfile_mkdtemp

from nexus import obsidian as ob
from nexus import register_edit as re_
from nexus.agent_tools import AgentTools
from nexus.ingest.register import import_register
from nexus.native import NativeDocs, NativeError
from nexus.search import ensure_index
from test_register_import import SCOPE, export

ob.SETTLE_SECONDS = 0.0
ob.NEW_NOTE_SETTLE = 0.0
COURSE = "reg/t100"


class RegisterEdit(NexusTest):

    def setUp(self):
        super().setUp()
        import_register(self.store, export("1"), SCOPE, "T100")
        ensure_index(self.conn)
        self.nd = NativeDocs(self.conn)
        self.t = AgentTools(self.core, SCOPE)

    def state(self, node="shape"):
        return self.t.nexus_learning_context(node)["node"]["learner_state"]["capability_state"]

    def test_read_only_until_migrated(self):
        with self.assertRaises(NativeError) as e:
            re_.edit_fields(self.nd, f"{COURSE}/shape", "learner_state", "user", set_fields={"capability_state": "X"})
        self.assertEqual(e.exception.code, "MIRROR_READ_ONLY")
        with self.assertRaises(NativeError):
            re_.migrate(self.nd, COURSE, "agent:ds")

    def test_migrate_then_edit_add_and_importer_refuses(self):
        out = re_.migrate(self.nd, COURSE, "user")
        self.assertGreater(out["migrated_objects"], 5)
        self.assertTrue(out["from"].startswith("GOOGLE_DRIVE:"))
        self.assertEqual(re_.migrate(self.nd, COURSE, "user")["migrated_objects"], 0)      # idempotent
        self.assertEqual(re_.binding(self.conn, f"{COURSE}/shape")["source_of_truth"], f"NEXUS:{COURSE}/shape")
        # edit a Learner_State cell -> learning context sees it; the old revision stays
        r = re_.edit_fields(self.nd, f"{COURSE}/shape", "learner_state", "user", expected_revision=1,
                            set_fields={"capability_state": "INDEPENDENT"})
        self.assertEqual((r["revision"], r["changed"]), (2, ["capability_state"]))
        self.assertEqual(self.state(), "INDEPENDENT")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM aspects WHERE object_id = ? AND name ="
                                           " 'learner_state'", (f"{COURSE}/shape",)).fetchone()[0], 2)
        ctx = self.t.nexus_learning_context("shape")
        self.assertEqual(ctx["register"]["source_of_truth"], "NEXUS")
        with self.assertRaises(NativeError) as e:                       # stale base
            re_.edit_fields(self.nd, f"{COURSE}/shape", "learner_state", "user", expected_revision=1,
                            set_fields={"capability_state": "Y"})
        self.assertEqual(e.exception.code, "VERSION_CONFLICT")
        # label change renames like the importer names
        re_.edit_fields(self.nd, f"{COURSE}/shape", "register_row", "user", set_fields={"canonical_label": "Pulse shape"})
        self.assertEqual(self.conn.execute("SELECT display_name FROM objects WHERE object_id = ?",
                                           (f"{COURSE}/shape",)).fetchone()[0], "shape｜Pulse shape")
        # a new knowledge row shows up in the overview
        re_.add_row(self.nd, COURSE, "knowledge", "nyquist", "user", fields={"canonical_label": "Nyquist criterion"})
        ids = {n["id"] for n in self.t.nexus_learning_context("")["knowledge_nodes"]}
        self.assertIn(f"{COURSE}/nyquist", ids)
        with self.assertRaises(ValueError):
            import_register(self.store, export("2"), SCOPE, "T100")


class RegisterInObsidian(NexusTest):

    def setUp(self):
        super().setUp()
        import_register(self.store, export("1"), SCOPE, "T100")
        ensure_index(self.conn)
        self.nd = NativeDocs(self.conn)
        root = Path(tempfile_mkdtemp())
        self.vault, self.statefile = root / "vault", root / "state.json"
        self.vault.mkdir()

    def sync(self):
        self.b = ob.Bridge(self.conn, SCOPE, self.vault, self.statefile)
        return self.b.sync_once()

    def path(self, oid):
        return self.vault / self.b.state["notes"][oid]["path"]

    def put(self, p, text):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        os.utime(p, (time.time() - 100,) * 2)

    def test_rows_are_property_notes_with_bases_and_become_editable_after_cutover(self):
        self.sync()
        p = self.path(f"{COURSE}/shape")
        self.assertEqual(p.relative_to(self.vault).as_posix(), "登记表/T100/knowledge/shape｜Pulse shaping.md")
        t = p.read_text(encoding="utf-8")
        self.assertIn('canonical_label: "Pulse shaping"', t)
        self.assertIn('ls_capability_state: "EXPOSED"', t)
        self.assertIn("nexus_readonly", t)
        base = (self.vault / "登记表/T100/knowledge.base").read_text(encoding="utf-8")
        self.assertIn('file.inFolder("登记表/T100/knowledge")', base)
        self.assertIn("note.ls_capability_state", base)
        # read-only: a property edit is restored and kept as a side copy
        self.put(p, t.replace('"EXPOSED"', '"INDEPENDENT"'))
        self.assertEqual(self.sync()["restored"], 1)
        self.assertIn('"EXPOSED"', p.read_text(encoding="utf-8"))
        # cutover -> notes re-render without the read-only marker
        re_.migrate(self.nd, COURSE, "user")
        self.sync()
        t = p.read_text(encoding="utf-8")
        self.assertNotIn("nexus_readonly", t)
        # edit a property and write a body -> cells in Nexus
        self.put(p, t.replace('"EXPOSED"', '"INDEPENDENT"').rstrip("\n") + "\n\n今天独立做出了一道 pulse shaping 题。\n")
        st = self.sync()
        self.assertEqual(st["pushed"], 1, st)
        t = AgentTools(self.core, SCOPE)
        self.assertEqual(t.nexus_learning_context("shape")["node"]["learner_state"]["capability_state"], "INDEPENDENT")
        row = self.conn.execute("SELECT payload FROM aspects WHERE object_id = ? AND name = 'register_row' AND"
                                " is_current = 1", (f"{COURSE}/shape",)).fetchone()[0]
        self.assertIn("独立做出", row)
        self.assertFalse(any(self.sync().values()))
        # a new note in a tab folder = a new row
        self.put(self.vault / "登记表/T100/knowledge/nyquist｜Nyquist criterion.md",
                 '---\ncanonical_label: "Nyquist criterion"\nepistemic_state: "SUPPORTED"\n---\n')
        st = self.sync()
        self.assertEqual(st["created_there"], 1, st)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM objects WHERE object_id = ?",
                                               (f"{COURSE}/nyquist",)).fetchone())
        self.assertFalse(any(self.sync().values()))

    def test_same_cell_changed_on_both_sides_is_a_conflict(self):
        re_.migrate(self.nd, COURSE, "user")
        self.sync()
        p = self.path(f"{COURSE}/shape")
        re_.edit_fields(self.nd, f"{COURSE}/shape", "learner_state", "user", set_fields={"capability_state": "FRAGILE"})
        self.put(p, p.read_text(encoding="utf-8").replace('"EXPOSED"', '"INDEPENDENT"'))
        st = self.sync()
        self.assertEqual(st["conflicts"], 1, st)
        self.assertIn('"FRAGILE"', p.read_text(encoding="utf-8"))
        self.assertEqual(len(list(self.vault.rglob("*冲突*.md"))), 1)


class RegisterToolsForAgents(NexusTest):
    """ChatGPT / DSH record receipts and learning state through MCP after the cutover."""

    def setUp(self):
        super().setUp()
        import_register(self.store, export("1"), SCOPE, "T100")
        ensure_index(self.conn)
        self.conn.commit()

    def call(self, server, tool, args):
        import asyncio
        import json as _j
        res = asyncio.run(server.call_tool(tool, args))
        content = getattr(res, "content", None) or (res[0] if isinstance(res, tuple) else res)
        text = content[0].text if isinstance(content, list) else str(content)
        return _j.dumps(_j.loads(text), ensure_ascii=False, separators=(",", ":"))

    def test_receipt_and_state_via_mcp(self):
        from nexus.mcp_server import build_server
        server = build_server(self.db_path, SCOPE, agent_name="chatgpt")
        # still a Sheet mirror: refused
        self.assertIn("MIRROR_READ_ONLY", self.call(server, "nexus_register_add_row", {
            "course": COURSE, "type": "learner_receipt", "row_id": "R:002", "fields": {"date": "2026-11-11"}}))
        re_.migrate(NativeDocs(self.conn), COURSE, "user")
        self.conn.commit()
        out = self.call(server, "nexus_register_add_row", {
            "course": COURSE, "type": "learner_receipt", "row_id": "R:002",
            "fields": {"date": "2026-11-11", "knowledge_ids": "shape;nosuch", "evidence_class": "INDEPENDENT_ATTEMPT",
                       "verbatim_learner_receipt": "独立推出零 ISI 条件"}})
        self.assertIn('"OK"', out)
        self.assertIn("reg/t100/shape", out)
        self.assertIn("nosuch", out)
        t = AgentTools(self.core, SCOPE)
        rec = t.nexus_learning_context("shape")["receipts"]
        self.assertIn("reg/t100/receipt/R:002", [r["id"] for r in rec])
        # edit learner_state with the revision read first (learning_context returns it)
        self.assertEqual(t.nexus_learning_context("shape")["node"]["learner_state"]["revision"], 1)
        out = self.call(server, "nexus_register_edit", {"object_id": f"{COURSE}/shape", "aspect": "learner_state",
                                                        "expected_revision": 1,
                                                        "set_fields": {"capability_state": "INDEPENDENT",
                                                                       "latest_receipt_id": "R:002"}})
        self.assertIn('"revision":2', out.replace(" ", ""))
        self.assertEqual(t.nexus_learning_context("shape")["node"]["learner_state"]["capability_state"], "INDEPENDENT")
        ver = self.conn.execute("SELECT verification FROM aspects WHERE object_id = ? AND name = 'learner_state'"
                                " AND is_current = 1", (f"{COURSE}/shape",)).fetchone()[0]
        self.assertEqual(ver, "UNVERIFIED")
        # a new knowledge row gets its Learner_State on the first edit
        self.call(server, "nexus_register_add_row", {"course": COURSE, "type": "knowledge", "row_id": "nyquist",
                                                     "fields": {"canonical_label": "Nyquist criterion"}})
        out = self.call(server, "nexus_register_edit", {"object_id": f"{COURSE}/nyquist", "aspect": "learner_state",
                                                        "expected_revision": 0,
                                                        "set_fields": {"capability_state": "EXPOSED"}})
        self.assertIn('"created":true', out.replace(" ", ""))
        nodes = {n["id"]: n["capability_state"] for n in t.nexus_learning_context("")["knowledge_nodes"]}
        self.assertEqual(nodes[f"{COURSE}/nyquist"], "EXPOSED")
