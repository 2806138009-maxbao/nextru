"""nexus_resolve: Manifest resolution over imported domains, folder tree, self-declared status."""

from __future__ import annotations

import json
import unittest

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store
from nexus.ingest.doc_status import import_doc_status
from nexus.ingest.manifest import import_manifest
from nexus.native import NativeDocs
from nexus.resolve import resolve
from test_manifest_import import IDS, ManifestImport
from test_native_v0 import SCOPE

D = lambda k: "doc/" + IDS[k][:16]  # noqa: E731


class Resolve(ManifestImport):
    def setUp(self):
        super().setUp()
        extra = {"doc/LECTURELECTURE": ("DEMO0020 Lecture 3 notes", ["Lecture 3", "notes"]),
                 "doc/OLDPLANOLDPLANOL": ("EXU old plan", ["Old plan", f"状态：HISTORICAL｜有效计划：https://docs.google.com/document/d/NEWPLANNEWPLANNE_full_id/edit"]),
                 "doc/NEWPLANNEWPLANNE": ("EXU plan", ["Plan", "mission_id: MIS-TEST-20260101-001 | object_type: MISSION | mission_state: ACTIVE"]),
                 "doc/STRAYSTRAYSTRAY0": ("Unfiled note", ["just a note"])}
        for oid, (name, paras) in extra.items():
            self.conn.execute("INSERT INTO objects VALUES (?, 'document', ?, ?, '2026-11-10')", (oid, name, SCOPE))
            self.conn.execute("INSERT INTO source_refs(source_ref_id, source_system, native_id, revision, access_scope,"
                              " status) VALUES (?, 'GOOGLE_DRIVE', ?, 'v1', ?, 'OK')", (f"ref-{oid[4:]}", oid[4:] + "_x", SCOPE))
            for i, t in enumerate(paras):
                self.conn.execute(
                    "INSERT INTO observations(observation_id, object_id, dimension, statement, stance, detail,"
                    " source_ref_ids, created_at) VALUES (?, ?, 'document_paragraph', ?, 'CASE', ?, ?, '2026-11-10')",
                    (f"obs/{oid}@v1/{i:04d}", oid, t, json.dumps({"anchor": f"h0.{i + 1}-x{i}"}), json.dumps([f"ref-{oid[4:]}"])))
        self.conn.commit()
        import_manifest(self.conn, SCOPE, "doc/MANIFESTMANIFES")
        # folder tree, as the Drive structure import writes it (the owner writes here for brevity)
        n = NativeDocs(self.conn)
        mk = lambda name: n.create_object(SCOPE, "folder", name, "user")["object_id"]  # noqa: E731
        exu, courses, life, london = mk("EXU"), mk("Courses"), mk("Living Life"), mk("02｜伦敦生活")
        for a, b in ((D("exu"), exu), (courses, exu), ("doc/LECTURELECTURE", courses), ("doc/OLDPLANOLDPLANOL", exu),
                     ("doc/NEWPLANNEWPLANNE", exu), (D("life"), life), (london, life), (D("london"), london)):
            n.add_relation(SCOPE, a, "PART_OF", b, "user")
        import_doc_status(self.conn, SCOPE)

    # the inherited Manifest-import tests assume an empty store; they run in test_manifest_import
    test_declared_structure_is_imported_verified_and_quoted = None
    test_rerun_is_idempotent = None

    def r(self, target, search=None):
        return resolve(self.conn, SCOPE, target, search=search)

    def test_domain_by_name_and_owner_strategy(self):
        out = self.r("EXU")
        self.assertEqual(out["state"], "KNOWN")
        self.assertEqual((out["domain"]["domain"], out["domain"]["strategy"]), ("EXU", "EXTERNAL_REALITY_FIRST"))
        self.assertEqual(out["domain"]["current_owner"], D("exu"))
        self.assertIn("refreshed", out["domain"]["strategy_meaning"])
        sub = self.r("LONDON_LIFE")
        self.assertEqual((sub["domain"]["domain"], sub["domain"]["parent_domain"]), ("LONDON_LIFE", "LIFE"))

    def test_object_classified_by_folder_tree_or_declaration(self):
        out = self.r("doc/LECTURELECTURE")
        self.assertEqual((out["state"], out["domain"]["domain"]), ("KNOWN", "EXU"))
        self.assertIn("folder tree", out["steps"][1]["basis"])
        model = self.r(D("model"))   # route-critical object: declared BELONGS_TO wins
        self.assertEqual(model["domain"]["domain"], "LONDON_LIFE")
        self.assertIn("Pointer Manifest", model["steps"][1]["basis"])
        stray = self.r("doc/STRAYSTRAYSTRAY0")
        self.assertEqual((stray["state"], stray["domain_state"]), ("KNOWN", "UNKNOWN"))

    def test_declared_id_and_retired_target(self):
        out = self.r("MIS-TEST-20260101-001")
        self.assertEqual((out["most_specific"], out["domain"]["domain"]), ("doc/NEWPLANNEWPLANNE", "EXU"))
        old = self.r("doc/OLDPLANOLDPLANOL")
        self.assertEqual(old["target_state"], "STALE")
        self.assertEqual(old["most_specific"], "doc/NEWPLANNEWPLANNE")

    def test_unknown_is_not_guessed_and_discovery_is_labelled(self):
        self.assertEqual(self.r("something unrelated")["state"], "UNKNOWN")
        out = self.r("lecture three", search=lambda q: ["doc/LECTURELECTURE"])
        self.assertEqual(out["state"], "UNKNOWN")          # a search hit never decides the domain
        self.assertEqual(out["candidates"][0], {"object_id": "doc/LECTURELECTURE", "name": "DEMO0020 Lecture 3 notes",
                                                "domain": "EXU", "status": "UNDECLARED"})

    def test_conflicts_and_missing_owner(self):
        NativeDocs(self.conn).add_relation(SCOPE, "doc/LECTURELECTURE", "OWNS_CURRENT", self.r("EXU")["domain"]["domain_entity"], "user")
        out = self.r("EXU")
        self.assertEqual(out["state"], "CONFLICTED")
        self.assertEqual(out["conflicts"][0]["kind"], "MULTIPLE_CURRENT_OWNERS")

    def test_agent_tool(self):
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        out = tools.nexus_resolve("doc/LECTURELECTURE")
        self.assertEqual((out["status"], out["state"]), ("OK", "KNOWN"))
        self.assertTrue(any(w["object_id"] == D("exu") for w in out["working_set"]))

    def test_identity_card_on_read(self):
        import os
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        card = tools.nexus_read("doc/OLDPLANOLDPLANOL")["identity"]
        self.assertIn("HISTORICAL", card["what"])
        self.assertEqual(card["where"], "EXU")
        self.assertEqual((card["domain"]["key"], card["domain"]["current_owner"]), ("EXU", "exu"))
        self.assertEqual(card["lineage"]["superseded_by"][0]["object_id"], "doc/NEWPLANNEWPLANNE")
        self.assertIn("refreshed", card["domain"]["rule"])
        plan = tools.nexus_read("doc/NEWPLANNEWPLANNE")["identity"]
        self.assertTrue(plan["what"].startswith("MISSION | declares itself CURRENT"), plan["what"])
        self.assertEqual(plan["declared"], "mission_state: ACTIVE")
        lecture = tools.nexus_read("doc/LECTURELECTURE")["identity"]
        self.assertEqual(lecture["where"], "EXU › Courses")
        small = tools.nexus_read("doc/OLDPLANOLDPLANOL", view="card")
        self.assertEqual((small["view"], small["identity"]["lineage"]["superseded_by"][0]["object_id"]),
                         ("card", "doc/NEWPLANNEWPLANNE"))
        self.assertLessEqual(sum(len(x.get("text") or "") for x in small["paragraphs"]), 1500)
        os.environ["NEXUS_IDENTITY_CARD"] = "0"
        try:
            self.assertNotIn("identity", tools.nexus_read("doc/OLDPLANOLDPLANOL"))
        finally:
            del os.environ["NEXUS_IDENTITY_CARD"]


if __name__ == "__main__":
    unittest.main()
