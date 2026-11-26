"""Withholding a sealed document hides it from every agent tool and is fully reversible."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from helpers import NexusTest, tempfile_mkdtemp

from nexus.agent_tools import AgentTools
from nexus.archive import Archive
from nexus.ingest import FetchedDocument, ingest_document
from nexus.withhold import _update_corpus, restore, withhold

SCOPE = "research:wh"
PRIVATE = "人物索引｜私密\n张三的住址和电话都在这里。\n"
PUBLIC = "课程｜公开\n张三是课程助教。\n"


def doc(text, did):
    return FetchedDocument("GOOGLE_DRIVE", did, text.splitlines()[0], "1", "2026-11-07T00:00:00Z", None, text,
                           raw=text.encode("utf-8"))


class WithholdTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile_mkdtemp()) / "a"
        arch = Archive(self.root)
        ingest_document(self.store, doc(PRIVATE, "PRIV1"), SCOPE, object_id="doc/priv", archive=arch)
        ingest_document(self.store, doc(PUBLIC, "PUB1"), SCOPE, object_id="doc/pub", archive=arch)
        self.tools = AgentTools(self.core, SCOPE)

    def tearDown(self) -> None:
        for p in self.root.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IWRITE)
        super().tearDown()

    def ids(self, q):
        return {h["object_id"] for h in self.tools.nexus_search(q)["hits"]}

    def test_withhold_hides_and_restore_returns(self) -> None:
        self.assertEqual(self.ids("张三"), {"doc/priv", "doc/pub"})
        self.assertEqual(withhold(self.store, SCOPE, ["doc/priv", "doc/nope"]), ["doc/priv"])
        self.assertEqual(self.ids("张三"), {"doc/pub"})                 # index rebuilt, no stale rows
        self.assertEqual(self.tools.nexus_read("doc/priv")["status"], "NOT_FOUND")
        sealed = self.store.conn.execute("SELECT COUNT(*) FROM source_refs WHERE archive_path IS NOT NULL").fetchone()[0]
        self.assertEqual(sealed, 2)                                    # nothing deleted, still sealed
        self.assertEqual(restore(self.store, SCOPE, ["doc/priv"]), ["doc/priv"])
        self.assertEqual(self.ids("张三"), {"doc/priv", "doc/pub"})
        self.assertEqual(self.tools.nexus_read("doc/priv")["status"], "OK")

    def test_corpus_entry_moves_both_ways(self) -> None:
        p = Path(tempfile_mkdtemp()) / "corpus.json"
        p.write_text(json.dumps({"include": [{"id": "PRIV1", "name": "x"}, {"id": "PUB1", "name": "y"}],
                                 "excluded": []}), encoding="utf-8")
        self.assertEqual(_update_corpus(p, self.store, ["doc/priv"], True, "privacy review"), 1)
        c = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual([d["id"] for d in c["include"]], ["PUB1"])
        self.assertEqual(c["excluded"][0]["withheld"]["reason"], "privacy review")
        self.assertEqual(_update_corpus(p, self.store, ["doc/priv"], False, ""), 1)
        c = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(sorted(d["id"] for d in c["include"]), ["PRIV1", "PUB1"])
        self.assertNotIn("withheld", c["include"][-1])
