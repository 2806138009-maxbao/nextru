"""Two-way Obsidian <-> Nexus sync (nexus.obsidian)."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus import obsidian as ob
from nexus.db import connect, init_schema
from nexus.native import NativeDocs
from test_native_v0 import SCOPE, SPEC, fresh_db

ob.SETTLE_SECONDS = 0.0
ob.NEW_NOTE_SETTLE = 0.0


def blocks_of(md: str) -> list[dict]:
    return ob.parse_blocks(md)


class Codec(unittest.TestCase):
    def test_parse_render_round_trip(self):
        md = "Intro para.\n\n# A\n\nOne\nstill one\n\n## A1\n\n```\ncode\n\nmore\n```\n\n# B\n\nTwo"
        b = blocks_of(md)
        self.assertEqual([(x["kind"], x["depth"]) for x in b],
                         [("paragraph", 0), ("section", 0), ("paragraph", 1), ("section", 1),
                          ("paragraph", 2), ("section", 0), ("paragraph", 1)])
        self.assertEqual(b[4]["text"], "```\ncode\n\nmore\n```")
        self.assertEqual(blocks_of(ob.render_blocks(b)), b)

    def test_skipped_heading_levels_are_normalised(self):
        b = blocks_of("## Top\n\n#### Deep\n\ntext")
        self.assertEqual([x["depth"] for x in b], [0, 1, 2])

    def test_diff_ops(self):
        base = [{"node_id": f"n_{i}", **x} for i, x in enumerate(blocks_of("# A\n\np1\n\np2\n\n# B\n\np3"))]
        ops, touched = ob.diff_ops(base, blocks_of("# A\n\np1 edited\n\np2\n\n# B\n\np3"))
        self.assertEqual(ops, [{"op": "set_text", "node_id": "n_1", "body": "p1 edited"}])
        self.assertEqual(touched, {"n_1"})
        ops, _ = ob.diff_ops(base, blocks_of("# A\n\np1\n\np2\n\n# B\n\np3\n\n# C\n\nc1\n\nc2"))
        self.assertEqual([o["op"] for o in ops], ["insert"] * 3)
        self.assertEqual((ops[0]["parent_id"], ops[0]["after"]), (None, "n_3"))
        self.assertEqual((ops[1]["parent_id"], ops[1]["after"]), (ops[0]["ref"], None))
        self.assertEqual((ops[2]["parent_id"], ops[2]["after"]), (ops[0]["ref"], ops[1]["ref"]))
        ops, _ = ob.diff_ops(base, blocks_of("# A\n\np1\n\np2"))      # drop section B
        self.assertEqual(ops, [{"op": "delete", "node_id": "n_3"}])

    def test_ops_applied_reproduce_the_note(self):
        conn = connect(fresh_db())
        init_schema(conn)
        nd = NativeDocs(conn)
        doc = nd.create(SCOPE, SPEC, "user")["doc_id"]
        cases = ["# Background\n\nFirst paragraph about lumen tracking.\n\n# Findings\n\nFinding one.",
                 "Lead paragraph.\n\n# New\n\n## Sub\n\ntext\n\n# Background\n\nchanged",
                 "# Only\n\none",
                 "# Only\n\none\n\ntwo\n\n# Two\n\n## Deep\n\nthree\n\n# Three"]
        for md in cases:
            r = nd.read(doc)
            base = [{"node_id": n["node_id"], "kind": n["kind"], "depth": n["depth"],
                     "text": n.get("title") if n["kind"] == "section" else n["text"]} for n in r["nodes"]]
            ops, _ = ob.diff_ops(base, blocks_of(md))
            nd.edit(doc, r["revision"], ops, "user")
            got = [{"kind": n["kind"], "depth": n["depth"],
                    "text": n.get("title") if n["kind"] == "section" else n["text"]} for n in nd.read(doc)["nodes"]]
            self.assertEqual(got, blocks_of(md), md)
        conn.close()

    def test_front_matter_keeps_unknown_keys(self):
        text = '---\nnexus_id: "ndoc/x"\ntags:\n  - a\n  - b\ncustom: hello\n---\nbody'
        entries, body = ob.split_front_matter(text)
        self.assertEqual(ob.fm_get(entries, "nexus_id"), "ndoc/x")
        self.assertEqual(ob.fm_get(entries, "tags"), ["a", "b"])
        out = ob.build_front_matter([("nexus_id", "ndoc/y")], entries)
        self.assertIn("tags:\n  - a\n  - b\ncustom: hello", out)
        self.assertNotIn("ndoc/x", out)

    def test_links(self):
        self.assertEqual(ob.links_in("see [[Alpha]] and [[Beta|b]] and [[Gamma#h]] but not ![[img.png]]"),
                         ["Alpha", "Beta", "Gamma"])


class Bridge(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.conn = connect(self.db)
        init_schema(self.conn)
        self.nd = NativeDocs(self.conn)
        self.doc = self.nd.create(SCOPE, SPEC, "user")["doc_id"]
        root = Path(tempfile_mkdtemp())
        self.vault, self.state = root / "vault", root / "state.json"
        self.vault.mkdir()
        self.b = ob.Bridge(self.conn, SCOPE, self.vault, self.state)

    def tearDown(self):
        self.conn.close()

    def sync(self):
        # a fresh Bridge each time = the state file really carries everything
        self.b = ob.Bridge(self.conn, SCOPE, self.vault, self.state)
        return self.b.sync_once()

    def path(self, oid):
        return self.vault / self.b.state["notes"][oid]["path"]

    def text(self, oid):
        return self.path(oid).read_text(encoding="utf-8")

    def put(self, oid_or_rel, text):
        p = self.path(oid_or_rel) if oid_or_rel in self.b.state["notes"] else self.vault / oid_or_rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        t = time.time() - 100
        os.utime(p, (t, t))

    def test_export_then_idle(self):
        st = self.sync()
        self.assertEqual(st["created_here"], 2)          # the native doc + the mirrored Drive doc
        t = self.text(self.doc)
        self.assertIn('nexus_id: "' + self.doc, t)
        self.assertIn("# Background\n\nFirst paragraph about lumen tracking.\n\n## Detail", t)
        st = self.sync()
        self.assertFalse(any(st.values()), st)

    def test_local_edit_becomes_a_revision_with_same_node_ids(self):
        self.sync()
        before = {n["text"]: n["node_id"] for n in self.nd.read(self.doc)["nodes"] if n["kind"] == "paragraph"}
        t = self.text(self.doc).replace("Finding one: cadence matters.", "Finding one: cadence matters a lot.")
        t = t.replace("cadence matters a lot.\n", "cadence matters a lot.\n\n## Added\n\nBrand new paragraph.\n")
        self.put(self.doc, t)
        st = self.sync()
        self.assertEqual(st["pushed"], 1, st)
        r = self.nd.read(self.doc)
        self.assertEqual(r["revision"], 2)
        texts = {n.get("text") or n.get("title"): n["node_id"] for n in r["nodes"]}
        self.assertEqual(texts["Finding one: cadence matters a lot."],
                         before["Finding one: cadence matters."])
        self.assertIn("Brand new paragraph.", texts)
        self.assertFalse(any(self.sync().values()))

    def test_nexus_edit_is_pulled(self):
        self.sync()
        r = self.nd.read(self.doc)
        nid = next(n["node_id"] for n in r["nodes"] if n.get("text", "").startswith("Finding"))
        self.nd.edit(self.doc, 1, [{"op": "set_text", "node_id": nid, "body": "Changed in Nexus."}], "agent:x"
                     ) if False else NativeDocs(self.conn, agent_writes=True).edit(
            self.doc, 1, [{"op": "set_text", "node_id": nid, "body": "Changed in Nexus."}], "agent:x")
        st = self.sync()
        self.assertEqual(st["pulled"], 1)
        self.assertIn("Changed in Nexus.", self.text(self.doc))

    def test_both_sides_different_paragraphs_merge(self):
        self.sync()
        r = self.nd.read(self.doc)
        nid = next(n["node_id"] for n in r["nodes"] if n.get("text", "").startswith("Finding"))
        NativeDocs(self.conn, agent_writes=True).edit(
            self.doc, 1, [{"op": "set_text", "node_id": nid, "body": "Remote change."}], "agent:x")
        self.put(self.doc, self.text(self.doc).replace("First paragraph", "Local first paragraph"))
        st = self.sync()
        self.assertEqual((st["pushed"], st["conflicts"]), (1, 0), st)
        t = self.text(self.doc)
        self.assertIn("Remote change.", t)
        self.assertIn("Local first paragraph", t)

    def test_both_sides_same_paragraph_conflict(self):
        self.sync()
        r = self.nd.read(self.doc)
        nid = next(n["node_id"] for n in r["nodes"] if n.get("text", "").startswith("Finding"))
        NativeDocs(self.conn, agent_writes=True).edit(
            self.doc, 1, [{"op": "set_text", "node_id": nid, "body": "Remote change."}], "agent:x")
        self.put(self.doc, self.text(self.doc).replace("Finding one", "Local finding"))
        st = self.sync()
        self.assertEqual(st["conflicts"], 1, st)
        self.assertIn("Remote change.", self.text(self.doc))
        copies = list(self.vault.rglob("*冲突*.md"))
        self.assertEqual(len(copies), 1)
        self.assertIn("Local finding", copies[0].read_text(encoding="utf-8"))
        self.assertIn("nexus_conflict_of", copies[0].read_text(encoding="utf-8"))
        self.assertFalse(any(self.sync().values()))      # the copy is not synced

    def test_new_note_link_relation_and_delete(self):
        self.sync()
        self.put("Notes/My idea.md", "# Plan\n\nLinked to [[Native Test Article]] here.")
        st = self.sync()
        self.assertEqual(st["created_there"], 1, st)
        self.assertEqual(st["relations_added"], 1, st)
        new = next(i for i, n in self.b.state["notes"].items() if n["path"] == "Notes/My idea.md")
        self.assertEqual(self.b.state["notes"][new]["path"], "Notes/My idea.md")
        self.assertIn("nexus_id", self.text(new))
        rel = self.conn.execute("SELECT * FROM relations WHERE from_id = ? AND expired_at IS NULL", (new,)).fetchone()
        self.assertEqual((rel["relation_type"], rel["to_id"], rel["verification"]),
                         ("LINKS_TO", self.doc, "VERIFIED"))
        self.assertTrue(rel["provenance_refs"].startswith('["nexus:' + new))
        # target note shows the incoming relation in its callout
        self.sync()
        self.assertIn("← LINKS_TO [[My idea]]", self.text(self.doc))
        # remove the link -> relation removed
        self.put(new, self.text(new).replace("[[Native Test Article]]", "the article"))
        st = self.sync()
        self.assertEqual(st["relations_removed"], 1, st)
        # delete the note -> archived
        self.path(new).unlink()
        st = self.sync()
        self.assertEqual(st["archived"], 1)
        self.assertTrue(self.conn.execute("SELECT archived_at FROM native_documents WHERE doc_id = ?",
                                          (new,)).fetchone()[0])

    def test_deleting_a_callout_line_removes_the_relation(self):
        ent = self.nd.create_object(SCOPE, "person", "Alice", "user")["object_id"]
        self.nd.add_relation(SCOPE, self.doc, "ABOUT", ent, "user")
        self.sync()
        self.sync()                                   # second pass: links resolve to notes
        t = self.text(self.doc)
        self.assertIn("ABOUT → [[Alice]]", t)
        self.put(self.doc, "\n".join(l for l in t.split("\n") if "ABOUT" not in l))
        st = self.sync()
        self.assertEqual(st["relations_removed"], 1, st)
        self.assertNotIn("ABOUT", self.text(self.doc))

    def test_entity_note_both_ways(self):
        self.put("实体/person/Bob.md", "---\naliases:\n  - Bobby\n---\nA test person.")
        st = self.sync()
        self.assertEqual(st["created_there"], 1, st)
        oid = next(i for i in self.b.state["notes"] if i.startswith("ent/person/"))
        e = self.b._entity(oid)
        self.assertEqual((e["name"], e["aliases"], e["description"]), ("Bob", ["Bobby"], "A test person."))
        self.put(oid, self.text(oid).replace("A test person.", "Edited description."))
        st = self.sync()
        self.assertEqual(st["pushed"], 1, st)
        self.assertEqual(self.b._entity(oid)["description"], "Edited description.")
        self.nd.update_object(oid, "user", name="Robert")
        st = self.sync()
        self.assertEqual(st["pulled"], 1, st)
        self.assertTrue((self.vault / "实体/person/Robert.md").exists())
        self.assertFalse((self.vault / "实体/person/Bob.md").exists())

    def test_local_rename_changes_title(self):
        self.sync()
        p = self.path(self.doc)
        p.rename(p.with_name("Renamed Article.md"))
        os.utime(p.with_name("Renamed Article.md"), (time.time() - 100,) * 2)
        st = self.sync()
        self.assertEqual(st["pushed"], 1, st)
        self.assertEqual(self.nd.read(self.doc)["title"], "Renamed Article")

    def test_duplicate_titles_do_not_look_like_renames(self):
        twin = self.nd.create(SCOPE, SPEC, "user")["doc_id"]
        self.sync()
        self.assertTrue(self.path(twin).stem.endswith(" (2)") or self.path(self.doc).stem.endswith(" (2)"))
        st = self.sync()
        self.assertFalse(any(st.values()), st)
        self.assertEqual(self.nd.read(twin)["revision"], 1)

    def _mirror_setup(self):
        c = self.conn
        c.execute("INSERT INTO objects VALUES ('ent/folder/f1','folder','Course Notes',?, '2026-11-08')", (SCOPE,))
        c.execute("INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle,"
                  " verification, revision, is_current, updated_at) VALUES ('f1e','ent/folder/f1','entity',"
                  " '{\"name\":\"Course Notes\"}','1','CURRENT','VERIFIED',1,1,'2026-11-08')")
        c.execute("INSERT INTO relations(relation_id, from_id, relation_type, to_id, verification, scope,"
                  " provenance_refs, created_at) VALUES ('rel_f','doc/mirror1','PART_OF','ent/folder/f1',"
                  " 'VERIFIED',?, '[]','2026-11-08')", (SCOPE,))
        c.execute("INSERT INTO observations(observation_id, object_id, dimension, statement, stance, detail,"
                  " source_ref_ids, created_at) VALUES ('obs/doc/mirror1@v3/0001','doc/mirror1',"
                  " 'document_paragraph','Mirrored paragraph one.','CASE','{}','[\"ref-m1\"]','2026-11-08')")
        self.nd.add_relation(SCOPE, self.doc, "ABOUT", "doc/mirror1", "user")

    def test_drive_snapshot_is_read_only_and_in_its_folder(self):
        self._mirror_setup()
        self.sync()
        self.sync()
        rel = self.b.state["notes"]["doc/mirror1"]["path"]
        self.assertEqual(rel, "Drive/Course Notes/Mirrored Drive Doc.md")
        t = self.text("doc/mirror1")
        self.assertIn("Mirrored paragraph one.", t)
        self.assertIn("nexus_readonly", t)
        self.assertIn("← ABOUT [[Native Test Article]]", t)
        self.assertNotIn("Course Notes（", t)            # folder relations are the directory, not lines
        self.assertIn("ABOUT → [[Mirrored Drive Doc]]", self.text(self.doc))
        # a property-only edit is restored, edit kept as a side copy, nothing written to Nexus
        self.put("doc/mirror1", t.replace('snapshot: "v3"', 'snapshot: "v99"'))
        st = self.sync()
        self.assertEqual((st["restored"], st["pushed"], st["migrated"]), (1, 0, 0), st)
        self.assertIn('snapshot: "v3"', self.text("doc/mirror1"))
        self.assertTrue(any(self.vault.rglob("*本地改动*.md")))
        # delete the relation line -> the relation goes, the note is otherwise unchanged
        t = self.text("doc/mirror1")
        self.put("doc/mirror1", "\n".join(l for l in t.split("\n") if "ABOUT" not in l))
        st = self.sync()
        self.assertEqual((st["relations_removed"], st["restored"]), (1, 0), st)
        self.assertEqual(self.sync()["pulled"], 1)       # the other end's callout loses the line too
        self.assertNotIn("ABOUT", self.text(self.doc))
        self.assertFalse(any(self.sync().values()))

    def test_editing_a_drive_note_moves_the_document_into_nexus(self):
        self._mirror_setup()
        self.sync()
        self.sync()
        t = self.text("doc/mirror1")
        self.put("doc/mirror1", t.replace("Mirrored paragraph one.", "Mirrored paragraph one, now edited.\n\n# New part\n\nAdded here."))
        st = self.sync()
        self.assertEqual(st["migrated"], 1, st)
        new = next(i for i, n in self.b.state["notes"].items() if n["path"] == "Drive/Course Notes/Mirrored Drive Doc.md")
        self.assertTrue(new.startswith("ndoc/"))
        self.assertIn("doc/mirror1", self.b.state["tombstones"])
        h = self.nd.history(new)["revisions"]
        self.assertEqual([r["revision"] for r in h], [1, 2])
        self.assertIn("Mirrored paragraph one.", [x.get("text") for x in self.nd.read(new, 1)["nodes"]])
        r2 = self.nd.read(new)
        self.assertIn("Mirrored paragraph one, now edited.", [x.get("text") for x in r2["nodes"]])
        self.assertIn("New part", [x.get("title") for x in r2["nodes"]])
        c = self.conn
        # relations moved: the folder (directory) and ABOUT now hang on the native document
        live = {(r[0], r[1], r[2]) for r in c.execute("SELECT from_id, relation_type, to_id FROM relations"
                                                     " WHERE expired_at IS NULL AND (from_id = ? OR to_id = ?)", (new, new))}
        self.assertIn((new, "PART_OF", "ent/folder/f1"), live)
        self.assertIn((self.doc, "ABOUT", new), live)
        self.assertIn((new, "SUPERSEDES", "doc/mirror1"), live)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM relations WHERE expired_at IS NULL AND relation_type != 'SUPERSEDES'"
                                   " AND (from_id = 'doc/mirror1' OR to_id = 'doc/mirror1')").fetchone()[0], 0)
        st_ = json.loads(c.execute("SELECT payload FROM aspects WHERE object_id = 'doc/mirror1' AND name = 'doc_status'"
                                   " AND is_current = 1").fetchone()[0])
        self.assertEqual((st_["currency"], st_["superseded_by"]), ("SUPERSEDED", new))
        note = self.text(new)
        self.assertNotIn("nexus_readonly", note)
        self.assertIn(f'nexus_id: "{new}"', note)
        self.assertIn("SUPERSEDES → Mirrored Drive Doc（Drive 快照）", note)
        self.sync()
        self.assertIn("ABOUT → [[Mirrored Drive Doc]]", self.text(self.doc))   # same note name, now the native doc
        self.assertFalse(any(self.sync().values()))
        # and it is an ordinary two-way note now
        self.put(new, self.text(new).replace("Added here.", "Added here, twice."))
        self.assertEqual(self.sync()["pushed"], 1)
        self.assertEqual(self.nd.read(new)["revision"], 3)

    def test_unchanged_notes_are_not_rerendered(self):
        self.sync()
        calls = []
        orig = ob.Bridge.render

        def counting(self_, *a, **k):
            calls.append(a[0])
            return orig(self_, *a, **k)
        ob.Bridge.render = counting
        try:
            self.sync()
            self.assertEqual(calls, [])
            r = self.nd.read(self.doc)
            nid = next(n["node_id"] for n in r["nodes"] if n["kind"] == "paragraph")
            self.nd.edit(self.doc, r["revision"], [{"op": "set_text", "node_id": nid, "body": "x"}], "user")
            st = self.sync()
            self.assertEqual(st["pulled"], 1)
            self.assertEqual(set(calls), {self.doc})
        finally:
            ob.Bridge.render = orig

    def test_mass_delete_is_refused(self):
        for i in range(6):
            self.nd.create(SCOPE, {"title": f"Doc {i}", "children": ["x"]}, "user")
        self.sync()
        for p in list(self.vault.rglob("*.md")):
            p.unlink()
        st = self.sync()
        self.assertEqual(st["archived"], 0)


if __name__ == "__main__":
    unittest.main()
