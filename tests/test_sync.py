"""On-demand sync and the near-empty-export tab fallback, against fakes (no network)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from helpers import NexusTest, tempfile_mkdtemp

from nexus.archive import Archive
from nexus.db import Store, connect
from nexus.ingest import FetchedDocument
from nexus.ingest.sources import DriveSource
from nexus.sync import sync

SCOPE = "research:sync"


class FakeDrive:
    def __init__(self, docs: dict[str, dict]):
        self.docs = docs          # id -> {"version": "3", "text": "...", "trashed": False}
        self.fetched: list[str] = []

    def version(self, doc_id):
        d = self.docs[doc_id]
        if d.get("error"):
            raise OSError("boom")
        return {"version": d["version"], "modifiedTime": "2026-11-07T00:00:00Z", "trashed": d.get("trashed", False)}

    def fetch(self, doc_id):
        self.fetched.append(doc_id)
        d = self.docs[doc_id]
        return FetchedDocument("GOOGLE_DRIVE", doc_id, d["text"].splitlines()[0], d["version"],
                               "2026-11-07T00:00:00Z", None, d["text"], raw=d["text"].encode("utf-8"))


class SyncTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile_mkdtemp()) / "archive"

    def tearDown(self) -> None:
        for p in self.root.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IWRITE)
        super().tearDown()

    def run_sync(self, drive, corpus):
        return sync(self.db_path, SCOPE, "unused", str(self.root), corpus, jobs=2, log=lambda *_: None,
                    drive=drive)

    def test_new_unchanged_changed_trashed_and_errors(self) -> None:
        docs = {"A": {"version": "3", "text": "文档A｜标题\n第一段。\n"},
                "B": {"version": "5", "text": "文档B｜标题\n内容。\n"},
                "T": {"version": "2", "text": "已删除\n", "trashed": True},
                "E": {"version": "1", "text": "x\n", "error": True}}
        corpus = [{"id": k, "name": k} for k in docs]
        drive = FakeDrive(docs)
        r1 = self.run_sync(drive, corpus)
        self.assertEqual(sorted(x["id"] for x in r1["new"]), ["A", "B"])
        self.assertEqual([x["id"] for x in r1["trashed"]], ["T"])
        self.assertEqual([x["id"] for x in r1["errors"]], ["E"])
        drive.fetched.clear()
        r2 = self.run_sync(drive, corpus)
        self.assertEqual((r2["unchanged"], drive.fetched), (2, []))   # version check only
        docs["A"] = {"version": "4", "text": "文档A｜标题\n第一段。\n新增一段。\n"}
        r3 = self.run_sync(drive, corpus)
        self.assertEqual([x["id"] for x in r3["changed"]], ["A"])
        store = Store(connect(self.db_path))
        revs = sorted(r[0] for r in store.conn.execute("SELECT revision FROM source_refs WHERE native_id='A'"))
        self.assertEqual(revs, ["3", "4"])                               # earlier revision kept
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM source_refs WHERE archive_path IS NULL"
                                            " AND native_id IN ('A','B')").fetchone()[0], 0)
        self.assertEqual(Archive(self.root).verify()["status"], "OK")

    def test_dry_run_fetches_nothing(self) -> None:
        drive = FakeDrive({"A": {"version": "3", "text": "A\n"}})
        rep = sync(self.db_path, SCOPE, "unused", str(self.root), [{"id": "A"}], dry_run=True,
                   log=lambda *_: None, drive=drive)
        self.assertEqual((drive.fetched, [w["id"] for w in rep["would_fetch"]]), ([], ["A"]))


class TabFallbackTests(NexusTest):

    def _source(self, export: bytes, tabs: dict) -> DriveSource:
        src = DriveSource("D1", ".", lossless=False)
        src._access_token = lambda: "tok"

        def get(url, token):
            if "docs.googleapis.com" in url:
                return json.dumps(tabs).encode()
            if "export?mimeType=text/plain" in url:
                return export
            return json.dumps({"id": "D1", "name": "n", "version": "2", "modifiedTime": "t"}).encode()
        src._get = staticmethod(get)
        return src

    def test_empty_export_rebuilt_from_tabs(self) -> None:
        para = lambda t: {"paragraph": {"elements": [{"textRun": {"content": t}}]}}
        tabs = {"tabs": [{"tabProperties": {"title": "Tab 1"}, "documentTab": {"body": {"content": [para("\n")]}}},
                         {"tabProperties": {"title": "正文"}, "documentTab": {"body": {"content": [
                             para("第二个标签页里的正文。\n"),
                             {"table": {"tableRows": [{"tableCells": [{"content": [para("格1\n")]},
                                                                      {"content": [para("格2\n")]}]}]}}]}}}]}
        d = self._source("﻿\r\n".encode("utf-8"), tabs).fetch()
        self.assertIn("第二个标签页里的正文。", d.text)
        self.assertIn("格1\t格2", d.text)
        self.assertEqual([r.filename for r in d.renditions], ["export.plain-original.txt"])
        self.assertTrue(any("all document tabs" in n for n in d.notes))
        root = Path(tempfile_mkdtemp()) / "a"
        rec = Archive(root).put(d)                      # archive accepts the rebuilt text
        self.assertEqual(Archive(root).verify()["status"], "OK")
        for p in root.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IWRITE)
        self.assertTrue(rec.created)

    def test_truly_empty_doc_is_noted_not_changed(self) -> None:
        tabs = {"tabs": [{"tabProperties": {"title": "Tab 1"},
                          "documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "\n"}}]}}]}}}]}
        d = self._source(b"\xef\xbb\xbf\r\n", tabs).fetch()
        self.assertEqual(d.renditions, ())
        self.assertTrue(any("hold no more text" in n for n in d.notes))

    def test_normal_export_skips_tab_check(self) -> None:
        d = self._source("正常导出的一段足够长的正文内容，超过二十个字符。\n".encode("utf-8"), {"tabs": []}).fetch()
        self.assertEqual(d.notes, ())
