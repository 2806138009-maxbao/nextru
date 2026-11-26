"""Ideas: the principle library becomes idea entities that come up when a situation matches."""

from __future__ import annotations

import json
import os
import unittest

from helpers import tempfile_mkdtemp  # noqa: F401  (also puts src/ on sys.path)

from nexus import search as si
from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store, connect, init_schema
from nexus.ideas import LIBRARY, associate, import_library, parse_library
from test_native_v0 import SCOPE, fresh_db

LIB = [
    ("h0.1-a", "先启动，再判断"), ("h0.2-b", "给到伦敦后的自己："), ("h0.3-c", "先开始几分钟，再决定要不要继续。"),
    ("h9.0-d", "09｜足够周全，不等于保证无误"), ("h9.1-e", "From State"), ("h9.2-f", "已经能区分证据与保证的你。"),
    ("h9.3-g", "To State"), ("h9.4-h", "做完决定后仍想不断检查的你。"),
    ("h9.5-i", "For When"), ("h9.6-j", "已核实关键事实，却仍反复想：万一之后出了意外，是不是证明我今天选错了？"),
    ("h9.7-k", "Message"), ("h9.8-l", "你已经考虑得足够周全了。"),
    ("h9.9-m", "Origin｜2026-10-15｜由机票讨论选定。"),
    ("h13.0-n", "13｜成绩是工具，不是我必须赢的比赛"), ("h13.5-o", "For When"),
    ("h13.6-p", "对 Scenario、Coursework 或考试成绩纠结的时候。"), ("h13.7-q", "Message"),
    ("h13.8-r", "60 分左右可以是值得接受的目标。"),
]


class Ideas(unittest.TestCase):
    def setUp(self):
        self.path = fresh_db()
        self.conn = connect(self.path)
        init_schema(self.conn)
        self.conn.execute("INSERT INTO objects VALUES (?, 'document', '00｜原则库｜Messages to Self', ?, '2026-11-10')",
                          (LIBRARY, SCOPE))
        self.conn.execute("INSERT INTO source_refs(source_ref_id, source_system, native_id, revision, access_scope,"
                          " status) VALUES ('ref-lib', 'GOOGLE_DRIVE', 'lib_full', 'v75', ?, 'OK')", (SCOPE,))
        for i, (a, t) in enumerate(LIB):
            self.conn.execute(
                "INSERT INTO observations(observation_id, object_id, dimension, statement, stance, detail,"
                " source_ref_ids, created_at) VALUES (?, ?, 'document_paragraph', ?, 'CASE', ?, '[\"ref-lib\"]', '2026-11-10')",
                (f"obs/{LIBRARY}@v75/{i:04d}", LIBRARY, t, json.dumps({"anchor": a})))
        self.conn.commit()
        si.ensure_index(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_parse_keeps_the_users_fields(self):
        ps = {p["number"]: p for p in parse_library(LIB)}
        self.assertEqual(sorted(ps), [1, 9, 13])
        self.assertEqual(ps[9]["title"], "足够周全，不等于保证无误")
        self.assertIn("万一", ps[9]["for_when"]["text"])
        self.assertEqual(ps[9]["message"]["anchor"], "h9.8-l")
        self.assertIn("先开始几分钟", ps[1]["message"]["text"])

    def test_import_and_association(self):
        out = import_library(self.conn, SCOPE)
        self.assertEqual(out["ideas"], 3)
        self.assertEqual(import_library(self.conn, SCOPE)["existing"], 3)          # idempotent
        self.assertEqual(self.conn.execute("SELECT value FROM search_meta WHERE key='fingerprint'").fetchone()[0],
                         si._fingerprint(self.conn))
        got = associate(self.conn, SCOPE, "Scenario X 可能拿 0 分，我是不是该重新考虑回国？")
        self.assertEqual([g["title"][:5] for g in got], ["原则 13", "原则 09"])
        self.assertIn("Scenario", got[0]["for_when"])
        self.assertEqual(associate(self.conn, SCOPE, "Law Report 什么时候截止？"), [])  # no noise
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        self.assertTrue(tools.nexus_search("成绩 60 分 要不要加码").get("ideas"))
        self.assertTrue(tools.nexus_search("原则 13")["hits"])                       # findable by alias
        os.environ["NEXUS_IDEAS"] = "0"
        try:
            self.assertNotIn("ideas", tools.nexus_search("成绩 60 分 要不要加码"))
        finally:
            del os.environ["NEXUS_IDEAS"]

    def test_task_ideas_come_first_from_the_learning_entry_call(self):
        import_library(self.conn, SCOPE)
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        self.assertNotIn("method", tools.nexus_learning_context("anything"))      # none declared: unchanged
        oid = self.conn.execute("SELECT object_id FROM aspects WHERE name='idea' AND payload LIKE '%\"number\": 9,%'"
                                ).fetchone()[0]
        p = json.loads(self.conn.execute("SELECT payload FROM aspects WHERE object_id=? AND name='idea'",
                                         (oid,)).fetchone()[0])
        p.update(tasks=["learning"], bound_notes={LIBRARY: "where it was written"},
                 links=[{"title": "a PDF", "url": "https://drive.google.com/file/d/abc/view"}])
        self.conn.execute("UPDATE aspects SET payload=? WHERE object_id=? AND name='idea'",
                          (json.dumps(p, ensure_ascii=False), oid))
        self.conn.execute("UPDATE source_refs SET location='https://docs.google.com/document/d/lib_full/edit'"
                          " WHERE source_ref_id='ref-lib'")
        from nexus.ideas import bind
        from nexus.native import NativeDocs
        with self.assertRaises(ValueError):
            bind(self.conn, SCOPE, oid, LIBRARY, "PART_OF")
        rid = bind(self.conn, SCOPE, oid, LIBRARY, "SUPPORTS")["relation_id"]
        out = tools.nexus_learning_context("a topic that matches no cue")          # no register here: NOT_FOUND
        self.assertEqual(out["status"], "NOT_FOUND")
        self.assertEqual(next(iter(out)), "method")
        self.assertEqual([m["idea"] for m in out["method"]], [oid])
        self.assertIn("足够周全", out["method"][0]["message"])                        # full text, not a pointer
        b = out["method"][0]["bound"]                                              # bound content, with a way in
        self.assertEqual([(x["object_id"], x["role"][:6], x["note"]) for x in b],
                         [(LIBRARY, "source", "where it was written")])
        self.assertEqual(b[0]["url"], "https://docs.google.com/document/d/lib_full/edit")
        self.assertEqual(out["method"][0]["links"][0]["url"], "https://drive.google.com/file/d/abc/view")
        rel = tools.nexus_related(oid)                                             # and visible as edges
        self.assertIn("SUPPORTS", json.dumps(rel, ensure_ascii=False))
        NativeDocs(self.conn).remove_relation(rid, "import:principles")            # unbinding is one call
        self.assertNotIn("bound", tools.nexus_learning_context("x")["method"][0])
        os.environ["NEXUS_IDEAS"] = "0"
        try:
            self.assertNotIn("method", tools.nexus_learning_context("x"))
        finally:
            del os.environ["NEXUS_IDEAS"]

    def test_add_idea_writes_revises_and_keeps_the_index_fresh(self):
        from nexus.ideas import add_idea, for_task
        tools = AgentTools(NexusCore(Store(self.conn)), SCOPE)
        tools.nexus_search("anything")                                            # index built and fresh
        spec = {"key": "mother-problem", "number": 101, "title": "母题学习法", "tasks": ["learning"],
                "fields": {"message": "选一道完整题带着学", "for_when": "开始学新内容"}, "cues": ["母题"],
                "evidence": {"object_id": LIBRARY, "anchor": "h9.8-l"},
                "bound": [{"object_id": LIBRARY, "relation": "SUPPORTS", "note": "出处"}]}
        a = add_idea(self.conn, SCOPE, spec)
        self.assertTrue(a["index_fresh"])                                         # next search does not rebuild
        self.assertTrue(tools.nexus_search("母题学习法")["hits"])
        spec["fields"]["message"] = "按知识点选一道完整题带着学"
        b = add_idea(self.conn, SCOPE, spec)
        self.assertEqual((b["idea"], b["revision"], b["relations"]), (a["idea"], 2, a["relations"]))
        got = for_task(self.conn, SCOPE, "learning")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["message"].startswith("按知识点"))
        self.assertEqual(got[0]["bound"][0]["note"], "出处")


if __name__ == "__main__":
    unittest.main()
