"""Course CORE REGISTER (Google Sheet) -> typed Nexus objects. Synthetic workbook, no real course data."""

from __future__ import annotations

import io
import json

from helpers import NexusTest

from nexus.agent_tools import AgentTools
from nexus.archive import Archive
from nexus.ingest.register import SheetExport, import_register, parse_workbook
from nexus.search import ensure_index

SCOPE = "research:reg"
SHEET = "SIMSHEET000000000001"


def workbook(*, shape_state="EXPOSED", drop_edge=False, extra_edge=None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "00 学习主页"
    ws.append(["课程主页（自由排版，无机器表头）"])
    ws.append(["当前单元：C2 Pulse Shaping"])
    o = wb.create_sheet("01 Objects")
    o.append(["object_id", "object_type", "canonical_label", "evidence_refs", "epistemic_state"])
    o.append(["fourier", "KNOWLEDGE", "Fourier transforms", None, "SUPPORTED"])
    o.append(["shape", "KNOWLEDGE", "Pulse shaping", None, "SUPPORTED"])
    o.append(["ex4", "QUESTION", "Sheet 1 Ex4\nsinc PSD", "E_ex4", "SUPPORTED_SOURCE"])
    r = wb.create_sheet("02 Relations")
    r.append(["edge_id", "from_object", "relation_type", "to_object", "support_type", "rationale"])
    if not drop_edge:
        r.append(["C:L:001", "fourier", "PREREQUISITE_OF", "shape", "DERIVED", "calc prerequisite"])
    r.append(["C:L:002", "ex4", "PRACTISES", "shape", "DIRECT", "zero-ISI proof NOT asked"])
    r.append(["C:L:003", "ex4", "TESTS", "ghost", "DIRECT", "endpoint missing"])
    if extra_edge:
        r.append(extra_edge)
    e = wb.create_sheet("03 Evidence")
    e.append(["evidence_id", "source_url", "anchor", "evidence_depth"])
    e.append(["E_ex4", "https://example.invalid/ps1.pdf", "Exercise 4, PDF p2", "INTERPRETED"])
    s = wb.create_sheet("04 Learner_State")
    s.append(["knowledge_id", "capability_state", "next_independent_evidence_gate"])
    s.append(["fourier", "FRAGILE", "unseen sketch"])
    s.append(["shape", shape_state, "unseen pulse task"])
    sim = wb.create_sheet("09 Current_State_SIM")
    sim.append(["knowledge_id", "capability_state"])
    sim.append(["shape", "SIMULATED_PASS"])
    rc = wb.create_sheet("05 Learner Receipts")
    rc.append(["receipt_id", "date", "knowledge_ids", "evidence_class", "verbatim_learner_receipt"])
    rc.append(["R:001", "2026-10-18", "fourier;shape", "DERIVED_PRACTICE", "pA(T)=0 判断正确"])
    nav = wb.create_sheet("09 Knowledge Navigator")
    nav.append(["node", "note"])
    nav.append(["1", "x"])                      # "1 | note: x" must not become a heading
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export(version: str, **kw) -> SheetExport:
    return SheetExport(SHEET, "TEST｜CORE REGISTER", version, f"2026-11-0{version}T00:00:00Z",
                       "sim://sheet", workbook(**kw))


class RegisterImportTests(NexusTest):

    def imp(self, version="1", **kw):
        rep = import_register(self.store, export(version, **kw), SCOPE, "T100",
                              archive=Archive(self._tmp_name + "/archive"))
        ensure_index(self.conn)
        return rep

    def tools(self):
        return AgentTools(self.core, SCOPE)

    def test_parse_detects_tabs_by_header_and_skips_sim(self):
        kinds = {t.title: t.kind for t in parse_workbook(workbook())}
        self.assertEqual(kinds["01 Objects"], "objects")
        self.assertEqual(kinds["02 Relations"], "relations")
        self.assertEqual(kinds["04 Learner_State"], "learner_state")
        self.assertEqual(kinds["05 Learner Receipts"], "receipts")
        self.assertIsNone(kinds["09 Current_State_SIM"])
        self.assertIsNone(kinds["00 学习主页"])

    def test_objects_relations_state_receipts_are_mirrored_with_row_quotes(self):
        rep = self.imp()
        self.assertEqual(rep["status"], "IMPORTED")
        c = self.conn
        self.assertEqual(c.execute("SELECT type FROM objects WHERE object_id='reg/t100/ex4'").fetchone()[0], "question")
        rel = c.execute("SELECT * FROM relations WHERE relation_id='reg/t100/C:L:002'").fetchone()
        self.assertEqual((rel["from_id"], rel["relation_type"], rel["to_id"], rel["verification"]),
                         ("reg/t100/ex4", "PRACTISES", "reg/t100/shape", "VERIFIED"))
        edges = self.tools().nexus_related("reg/t100/ex4")["edges"]
        e = next(x for x in edges if x["relation_id"] == "reg/t100/C:L:002")
        self.assertIn("zero-ISI proof NOT asked", e["evidence"][0]["quote"])
        anchor = e["evidence"][0]["anchor"]
        body = c.execute("SELECT body FROM source_sections WHERE anchor=?", (anchor,)).fetchone()[0]
        self.assertTrue(body.startswith("C:L:002 |"))
        # Learner_State is a Current fact with the Sheet as owner
        facts = self.tools().nexus_read("reg/t100/shape")["current_facts"]
        ls = next(f for f in facts if f["aspect"] == "learner_state")
        self.assertEqual((ls["status"], ls["value"]["value"]), ("OK", "EXPOSED"))
        self.assertEqual(ls["verification"], "VERIFIED")
        # receipt -> knowledge
        self.assertEqual(c.execute("SELECT COUNT(*) FROM relations WHERE from_id='reg/t100/receipt/R:001'"
                                   " AND relation_type='EVIDENCE_FOR'").fetchone()[0], 2)
        # evidence pointer
        self.assertEqual(c.execute("SELECT to_id FROM relations WHERE relation_type='EVIDENCED_BY'").fetchone()[0],
                         "reg/t100/ev/E_ex4")

    def test_missing_endpoint_is_reported_not_guessed(self):
        rep = self.imp()
        self.assertTrue(any(s.get("edge") == "C:L:003" and s["missing"] == ["ghost"] for s in rep["skipped"]))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM objects WHERE object_id='reg/t100/ghost'").fetchone())

    def test_sim_tab_is_text_only(self):
        self.imp()
        val = self.tools().nexus_read("reg/t100/shape")["current_facts"]
        self.assertNotIn("SIMULATED_PASS", json.dumps(val, ensure_ascii=False))
        hits = self.conn.execute("SELECT COUNT(*) FROM source_sections WHERE body LIKE '%SIMULATED_PASS%'").fetchone()[0]
        self.assertEqual(hits, 1)

    def test_multiline_cell_and_heading_like_row(self):
        self.imp()
        bodies = [r[0] for r in self.conn.execute("SELECT body FROM source_sections")]
        self.assertTrue(any("Sheet 1 Ex4 ⏎ sinc PSD" in b for b in bodies))
        self.assertIn("· 1 | note: x", bodies)

    def test_same_version_is_unchanged_and_writes_nothing(self):
        self.imp()
        n = self.conn.execute("SELECT COUNT(*) FROM aspects").fetchone()[0]
        self.assertEqual(self.imp()["status"], "UNCHANGED")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM aspects").fetchone()[0], n)

    def test_new_version_supersedes_state_and_rebuilds_relations(self):
        self.imp("1")
        rep = self.imp("2", shape_state="LOCAL_PASS", drop_edge=True,
                       extra_edge=["C:L:004", "shape", "TRANSFERS_TO", "fourier", "DERIVED", "x"])
        self.assertEqual(rep["status"], "IMPORTED")
        c = self.conn
        cur = c.execute("SELECT payload, lifecycle FROM aspects WHERE object_id='reg/t100/shape'"
                        " AND name='learner_state' AND is_current=1").fetchall()
        self.assertEqual(len(cur), 1)
        self.assertEqual(json.loads(cur[0]["payload"])["value"], "LOCAL_PASS")
        old = c.execute("SELECT lifecycle FROM aspects WHERE object_id='reg/t100/shape' AND name='learner_state'"
                        " AND is_current=0").fetchone()
        self.assertEqual(old["lifecycle"], "SUPERSEDED")
        self.assertIsNone(c.execute("SELECT 1 FROM relations WHERE relation_id='reg/t100/C:L:001'").fetchone())
        self.assertIsNotNone(c.execute("SELECT 1 FROM relations WHERE relation_id='reg/t100/C:L:004'").fetchone())
        # no stale quote left behind for the removed edge
        self.assertEqual(c.execute("SELECT COUNT(*) FROM observations WHERE detail LIKE '%C:L:001%'").fetchone()[0], 0)
        # the earlier revision is still sealed and readable
        self.assertEqual(c.execute("SELECT COUNT(*) FROM source_refs WHERE native_id=? AND archive_path IS NOT NULL",
                                   (SHEET,)).fetchone()[0], 2)

    def test_newer_sheet_head_makes_mirror_not_fresh(self):
        self.imp("1")
        self.store.set_source_head("GOOGLE_DRIVE", SHEET, "2", "2026-11-09T00:00:00Z")
        self.conn.commit()
        ls = next(f for f in self.tools().nexus_read("reg/t100/shape")["current_facts"]
                  if f["aspect"] == "learner_state")
        self.assertNotEqual(ls["freshness"], "FRESH")


class LearningContextTests(NexusTest):

    def setUp(self):
        super().setUp()
        import_register(self.store, export("1"), SCOPE, "T100")
        ensure_index(self.conn)
        self.t = AgentTools(self.core, SCOPE)

    def test_topic_resolves_to_node_with_state_prereqs_materials_receipts(self):
        r = self.t.nexus_learning_context("Pulse shaping")
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["node"]["id"], "reg/t100/shape")
        self.assertEqual(r["node"]["learner_state"]["capability_state"], "EXPOSED")
        self.assertTrue(r["node"]["learner_state"]["anchor"].startswith("h"))
        self.assertEqual([(p["id"], p["capability_state"]) for p in r["prerequisites"]],
                         [("reg/t100/fourier", "FRAGILE")])
        m = r["materials"][0]
        self.assertEqual((m["id"], m["relation"]), ("reg/t100/ex4", "PRACTISES"))
        self.assertEqual(m["edge_note"]["rationale"], "zero-ISI proof NOT asked")
        self.assertEqual([x["id"] for x in r["receipts"]], ["reg/t100/receipt/R:001"])
        self.assertIn("pA(T)=0", r["receipts"][0]["excerpt"])
        self.assertEqual(r["register"]["snapshot_version"], "1")

    def test_question_row_text_routes_to_its_knowledge_node(self):
        r = self.t.nexus_learning_context("sinc PSD")
        self.assertEqual(r["node"]["id"], "reg/t100/shape")

    def test_exact_node_id_and_empty_topic_overview(self):
        self.assertEqual(self.t.nexus_learning_context("fourier")["node"]["id"], "reg/t100/fourier")
        r = self.t.nexus_learning_context("")
        self.assertEqual({(n["id"], n["capability_state"]) for n in r["knowledge_nodes"]},
                         {("reg/t100/fourier", "FRAGILE"), ("reg/t100/shape", "EXPOSED")})

    def test_unknown_topic_and_missing_register(self):
        self.assertEqual(self.t.nexus_learning_context("photodiode responsivity")["status"], "NOT_FOUND")
        self.assertEqual(self.t.nexus_learning_context("shape", course="NOPE")["status"], "NOT_FOUND")

    def test_sim_rows_never_reach_learner_state(self):
        r = self.t.nexus_learning_context("shape")
        self.assertNotIn("SIMULATED_PASS", json.dumps(r["node"], ensure_ascii=False))

    def test_mcp_tool_calls_through(self):
        import asyncio
        from nexus.mcp_server import build_server
        self.conn.commit()
        server = build_server(self.db_path, SCOPE)
        res = asyncio.run(server.call_tool("nexus_learning_context", {"topic": "Pulse shaping"}))
        self.assertIn("reg/t100/shape", json.dumps(res, ensure_ascii=False, default=str))
