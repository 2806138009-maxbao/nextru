"""Task 008 S1 — standard ingestion: stable anchors, idempotency, versions.

Documents are synthetic; no real Drive text is stored in the repository.
"""

from __future__ import annotations

import sqlite3

from helpers import NexusTest, tempfile_mkdtemp

from nexus.db import Store, connect, init_schema
from nexus.errors import Status
from nexus.ingest import FetchedDocument, ingest_document, split_sections
from nexus.research import build_research_evidence

SCOPE = "research:t008"

DOC_V1 = """测试文档｜导入链路
LIVING / SYNTHETIC
一｜背景
第一段：先手机，后灯光。
第二段：灯光比麦克风更先买。
二｜方法
固定机位，加小标注 UI。
每个镜头 2–5 秒。
"""

# v2 edits heading 一 only; heading 二 paragraphs must keep their anchors.
DOC_V2 = DOC_V1.replace("第一段：先手机，后灯光。", "第一段：先手机，后灯光，再麦克风。\n插入的新段落。")


def doc(text: str, version: str) -> FetchedDocument:
    return FetchedDocument("GOOGLE_DRIVE", "SIMDOC0001", "测试文档", version,
                           "2026-11-01T00:00:00Z", "sim://doc/SIMDOC0001", text)


class SectionSplitTests(NexusTest):

    def test_headings_and_anchor_format(self) -> None:
        title, secs = split_sections(DOC_V1)
        self.assertEqual(title, "测试文档｜导入链路")
        by_body = {s.body: s for s in secs}
        self.assertTrue(by_body["一｜背景"].anchor.startswith("h1.0-"))
        self.assertTrue(by_body["第二段：灯光比麦克风更先买。"].anchor.startswith("h1.2-"))
        self.assertTrue(by_body["每个镜头 2–5 秒。"].anchor.startswith("h2.2-"))
        self.assertEqual(len({s.anchor for s in secs}), len(secs))

    def test_anchor_stable_when_other_heading_edited(self) -> None:
        _, v1 = split_sections(DOC_V1)
        _, v2 = split_sections(DOC_V2)
        a1 = {s.body: s.anchor for s in v1 if s.heading_no == 2}
        a2 = {s.body: s.anchor for s in v2 if s.heading_no == 2}
        self.assertEqual(a1, a2)
        self.assertNotEqual(
            {s.anchor for s in v1 if s.heading_no == 1},
            {s.anchor for s in v2 if s.heading_no == 1})

    def test_long_line_with_bar_is_not_a_heading(self) -> None:
        text = "标题\n" + "一｜" + "很长的正文" * 20 + "\n"
        _, secs = split_sections(text)
        self.assertEqual(secs[1].heading_no, 0)


class IngestPipelineTests(NexusTest):

    def test_ingest_then_research_read_is_complete(self) -> None:
        out = ingest_document(self.store, doc(DOC_V1, "1"), SCOPE, object_id="doc/sim")
        self.assertEqual(out["status"], "INGESTED")
        env = build_research_evidence(self.core, "doc/sim", SCOPE, max_chars=50000)
        self.assertStatus(env, Status.OK)
        reading = env["result"]["readings"][0]
        self.assertTrue(reading["coverage"]["complete"])
        self.assertEqual(reading["coverage"]["segment_count"], out["sections"])
        anchors = [s["anchor"] for s in reading["segments"]]
        self.assertTrue(all(a.startswith("h") for a in anchors))
        # document order is preserved (observation ids carry the ordinal)
        self.assertEqual(reading["segments"][0]["text"], "测试文档｜导入链路")

    def test_reingest_same_revision_writes_nothing(self) -> None:
        ingest_document(self.store, doc(DOC_V1, "1"), SCOPE, object_id="doc/sim")
        before = self.conn.execute("SELECT COUNT(*), MAX(retrieved_at) FROM source_refs").fetchone()
        again = ingest_document(self.store, doc(DOC_V1, "1"), SCOPE, object_id="doc/sim")
        self.assertEqual(again["status"], "UNCHANGED")
        after = self.conn.execute("SELECT COUNT(*), MAX(retrieved_at) FROM source_refs").fetchone()
        self.assertEqual(tuple(before), tuple(after))

    def test_new_version_supersedes_old_content_and_keeps_history(self) -> None:
        ingest_document(self.store, doc(DOC_V1, "1"), SCOPE, object_id="doc/sim")
        out = ingest_document(self.store, doc(DOC_V2, "2"), SCOPE, object_id="doc/sim")
        self.assertEqual(out["content_revision"], 2)
        rows = self.conn.execute(
            "SELECT revision, lifecycle FROM aspects WHERE object_id='doc/sim' AND name='content'"
            " ORDER BY revision").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(1, "SUPERSEDED"), (2, "DRAFT")])
        head = self.store.get_source_head("GOOGLE_DRIVE", "SIMDOC0001")
        self.assertEqual(head["latest_revision"], "2")
        # old revision's sections are still traceable
        old_ref = "src/google-drive/SIMDOC0001@v1"
        self.assertEqual(len(self.store.get_sections(old_ref)), len(split_sections(DOC_V1)[1]))
        # nothing is promoted to Current
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM aspects WHERE object_id='doc/sim' AND is_current=1").fetchone()[0], 0)

    def test_aliases_resolve_legacy_and_hash(self) -> None:
        ingest_document(self.store, doc(DOC_V1, "1"), SCOPE, object_id="doc/sim")
        ref = "src/google-drive/SIMDOC0001@v1"
        _, secs = split_sections(DOC_V1)
        target = secs[4]
        self.assertEqual(self.store.resolve_anchor(ref, target.legacy), target.anchor)
        self.assertEqual(self.store.resolve_anchor(ref, target.content_sha256[:8]), target.anchor)
        self.assertEqual(self.store.resolve_anchor(ref, target.anchor), target.anchor)
        self.assertIsNone(self.store.resolve_anchor(ref, "p999"))


class MigrationTests(NexusTest):

    def test_old_database_gains_new_columns(self) -> None:
        path = f"{tempfile_mkdtemp()}/old.db"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE source_sections (source_ref_id TEXT NOT NULL, anchor TEXT NOT NULL,"
                    " heading TEXT, body TEXT NOT NULL, char_count INTEGER NOT NULL,"
                    " PRIMARY KEY (source_ref_id, anchor))")
        raw.commit()
        raw.close()
        conn = connect(path)
        init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(source_sections)")}
        self.assertTrue({"heading_path", "ordinal", "content_sha256"} <= cols)
        Store(conn)  # usable
        conn.close()
