"""Pointer Manifest import: declared domains, owners and pointers become quoted, idempotent structure."""

from __future__ import annotations

import json
import unittest

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store, connect, init_schema
from nexus.ingest.manifest import ACTOR, import_manifest
from test_native_v0 import SCOPE, fresh_db

IDS = {"root": "ROOTROOTROOTROOT_x", "spine": "SPINESPINESPINE0_x", "exu": "UCLINDEXUCLINDEX_x",
       "life": "LIFEINDEXLIFEIND_x", "london": "LONDONIDXLONDONI_x", "model": "LONDONMODELLONDO_x"}
BLOCK = [
    "BEGIN_REPOSITORY_CONTRACT_JSON", "{",
    '"contract_id": "REP-CONTRACT-TEST",',
    '"root_entry_ref": {', '"role": "ROOT_ROUTER",', f'"id": "{IDS["root"]}"', "},",
    '"semantic_standard_ref": {', f'"id": "{IDS["spine"]}"', "},",
    '"capability_registry_ref": {', '"id": "NOTINNEXUSNOTINN_x"', "},",
    '"root_objects": {',
    '"EXU": {', '"kind": "DOMAIN",',
    f'"index_ref": {{"provider":"google_drive","id":"{IDS["exu"]}"}},',
    '"current_owner_strategy": "EXTERNAL_REALITY_FIRST",',
    f'"current_owner_ref": {{"provider":"google_drive","id":"{IDS["exu"]}"}},',
    '"authority_ref": "semantic_standard_ref",', '"manifest_state": "READY"', "},",
    '"LIFE": {', '"kind": "DOMAIN",',
    f'"index_ref": {{"provider":"google_drive","id":"{IDS["life"]}"}},',
    '"current_owner_strategy": "INDEX_ROUTE_TO_SPECIFIC",',
    f'"current_owner_ref": {{"provider":"google_drive","id":"{IDS["life"]}"}},',
    '"authority_ref": "semantic_standard_ref",', '"manifest_state": "READY"', "}", "},",
    '"subdomain_registry": {', '"LONDON_LIFE": {', '"parent_domain": "LIFE",',
    f'"index_ref": {{"provider":"google_drive","id":"{IDS["london"]}"}},',
    '"current_owner_strategy": "INDEX_ROUTE_TO_SPECIFIC",',
    f'"current_owner_ref": {{"provider":"google_drive","id":"{IDS["london"]}"}},',
    '"authority_ref": "semantic_standard_ref",', '"manifest_state": "READY"', "}", "},",
    '"route_critical_objects": {', '"LONDON_MODEL": {',
    f'"canonical_ref": {{"provider":"google_drive","id":"{IDS["model"]}"}},',
    '"relationships": [', '{"type":"BELONGS_TO","target":"LONDON_LIFE"},',
    '{"type":"DEPENDS_ON","target":"EXU"},', '{"type":"GOVERNED_BY","target":"REP-CONTRACT-TEST"}', "]", "}", "}",
    "}", "END_REPOSITORY_CONTRACT_JSON",
]


class ManifestImport(unittest.TestCase):
    def setUp(self):
        self.path = fresh_db()
        self.conn = connect(self.path)
        init_schema(self.conn)
        docs = {"doc/MANIFESTMANIFES": "Pointer Manifest"}
        docs.update({"doc/" + v[:16]: k for k, v in IDS.items()})
        for oid, name in docs.items():
            self.conn.execute("INSERT INTO objects VALUES (?, 'document', ?, ?, '2026-11-10')", (oid, name, SCOPE))
        self.conn.execute("INSERT INTO source_refs(source_ref_id, source_system, native_id, revision, access_scope,"
                          " status) VALUES ('ref-man','GOOGLE_DRIVE','MANIFESTMANIFES_full','v18',?, 'OK')", (SCOPE,))
        for i, text in enumerate(["Repository Pointer Manifest v0.1", "Status: FROZEN"] + BLOCK):
            self.conn.execute(
                "INSERT INTO observations(observation_id, object_id, dimension, statement, stance, detail,"
                " source_ref_ids, created_at) VALUES (?, 'doc/MANIFESTMANIFES', 'document_paragraph', ?, 'CASE', ?,"
                " '[\"ref-man\"]', '2026-11-10')",
                (f"obs/doc/MANIFESTMANIFES@v18/{i:04d}", text, json.dumps({"anchor": f"h0.{i}-abcd{i:04d}"})))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def run_import(self):
        return import_manifest(self.conn, SCOPE, "doc/MANIFESTMANIFES")

    def edges(self):
        return {(r[0], r[1], r[2]) for r in self.conn.execute(
            "SELECT from_id, relation_type, to_id FROM relations WHERE expired_at IS NULL")}

    def test_declared_structure_is_imported_verified_and_quoted(self):
        out = self.run_import()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["domains"], 3)
        dom = {r[1]: r[0] for r in self.conn.execute(
            "SELECT object_id, alias FROM entity_aliases WHERE alias LIKE 'domain:%'")}
        exu, life, london = dom["domain:EXU"], dom["domain:LIFE"], dom["domain:LONDON_LIFE"]
        spine = "doc/" + IDS["spine"][:16]
        e = self.edges()
        self.assertIn(("doc/MANIFESTMANIFES", "ROOT_ENTRY_REF", "doc/" + IDS["root"][:16]), e)
        self.assertIn((exu, "INDEX_REF", "doc/" + IDS["exu"][:16]), e)
        self.assertIn(("doc/" + IDS["exu"][:16], "OWNS_CURRENT", exu), e)
        self.assertIn((exu, "GOVERNED_BY", spine), e)
        self.assertIn((london, "BELONGS_TO", life), e)
        model = "doc/" + IDS["model"][:16]
        self.assertIn((model, "DEPENDS_ON", exu), e)
        self.assertIn((model, "GOVERNED_BY", "doc/MANIFESTMANIFES"), e)
        # the capability registry is not in this store: reported, never guessed
        self.assertTrue(any("capability_registry_ref" in x for x in out["not_in_nexus"]))
        # the contract's own declarations: VERIFIED, written by the importer, each with its quote
        rows = self.conn.execute("SELECT verification, extraction_run FROM relations").fetchall()
        self.assertEqual({tuple(r) for r in rows}, {("VERIFIED", ACTOR)})
        quote = self.conn.execute(
            "SELECT o.statement FROM observations o JOIN relations r ON o.detail LIKE '%' || r.relation_id || '%'"
            " WHERE r.from_id = ? AND r.relation_type = 'OWNS_CURRENT'", ("doc/" + IDS["exu"][:16],)).fetchone()[0]
        self.assertIn('"current_owner_ref"', quote)
        # the owner strategy is readable on the domain itself
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        r = tools.nexus_read(exu)
        self.assertIn("EXTERNAL_REALITY_FIRST", json.dumps(r, ensure_ascii=False))
        self.assertTrue(any(x["type"] == "OWNS_CURRENT" for x in r["edges"]))

    def test_rerun_is_idempotent(self):
        first = self.run_import()
        n = len(self.edges())
        again = self.run_import()
        self.assertEqual((again["domains"], again["edges"]), (0, 0))
        self.assertEqual(again["existing"], first["edges"])
        self.assertEqual(len(self.edges()), n)


if __name__ == "__main__":
    unittest.main()
