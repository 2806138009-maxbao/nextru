"""Write-once document archive and sealed source_refs.

Synthetic documents only; the Drive adapter is exercised against a fake HTTP layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

from helpers import NexusTest, tempfile_mkdtemp

from nexus.archive import Archive, ArchiveError
from nexus.db import sha256_text
from nexus.ingest import FetchedDocument, ingest_document
from nexus.ingest.sources import DOCX_MIME, DriveSource, LocalTextSource, Rendition
from nexus.research import build_research_evidence
from nexus.errors import Status

SCOPE = "research:archive"
TEXT = "封存测试｜文档\n一｜背景\n第一段。\n重复段。\n重复段。\n二｜方法\n第二段。\n"


def doc(text: str, version: str = "1", **kw) -> FetchedDocument:
    raw = kw.pop("raw", None)
    return FetchedDocument("GOOGLE_DRIVE", "SIMARCH001", "封存测试", version, "2026-11-07T00:00:00Z",
                           "sim://doc/SIMARCH001", text, raw=raw if raw is not None else
                           ("﻿" + text).encode("utf-8"), **kw)


class ArchiveTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile_mkdtemp()) / "archive"
        self.arch = Archive(self.root)

    def tearDown(self) -> None:  # read-only files must be made writable before cleanup
        for p in self.root.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IWRITE)
        super().tearDown()

    def test_put_is_write_once_read_only_and_verifiable(self) -> None:
        d = doc(TEXT, renditions=(Rendition("export.docx", DOCX_MIME, b"PK-fake-docx"),))
        rec = self.arch.put(d)
        self.assertTrue(rec.created)
        entry = self.root / rec.path
        self.assertEqual((entry / "export.txt").read_bytes(), d.raw)  # bytes as received, BOM kept
        self.assertEqual(rec.text_sha256, sha256_text(TEXT))          # == source_refs.content_sha256
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(set(meta["files"]), {"export.txt", "export.docx"})
        for name in ("export.txt", "export.docx", "meta.json"):
            self.assertFalse(os.access(entry / name, os.W_OK), name)
        again = self.arch.put(d)
        self.assertFalse(again.created)
        self.assertEqual(len((self.root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.arch.verify()["status"], "OK")

    def test_same_version_new_bytes_gets_second_entry_and_conflict_flag(self) -> None:
        a = self.arch.put(doc(TEXT))
        b = self.arch.put(doc(TEXT + "补一段。\n"))
        self.assertNotEqual(a.path, b.path)
        self.assertFalse(a.version_conflict)
        self.assertTrue(b.version_conflict)
        self.assertTrue((self.root / a.path / "export.txt").exists())
        self.assertEqual(self.arch.verify()["status"], "OK")

    def test_verify_detects_tampering_missing_and_unlisted(self) -> None:
        rec = self.arch.put(doc(TEXT))
        f = self.root / rec.path / "export.txt"
        os.chmod(f, stat.S_IREAD | stat.S_IWRITE)   # S_IWRITE alone is write-only on POSIX
        f.write_bytes(f.read_bytes() + b"x")
        rep = self.arch.verify()
        self.assertEqual(rep["status"], "PROBLEMS")
        self.assertIn("export.txt hash mismatch", rep["problems"][rec.path])
        self.assertIn("export.txt is writable", rep["problems"][rec.path])
        with self.assertRaises(ArchiveError):  # a damaged entry is never silently reused
            self.arch.put(doc(TEXT))

    def test_raw_that_does_not_decode_to_text_is_refused(self) -> None:
        with self.assertRaises(ArchiveError):
            self.arch.put(doc(TEXT, raw=b"something else"))

    def test_ingest_seals_ref_and_archive_text_matches_sections(self) -> None:
        out = ingest_document(self.store, doc(TEXT), SCOPE, object_id="doc/arch", archive=self.arch)
        self.assertEqual(out["status"], "INGESTED")
        ref = self.store.get_source_ref(out["source_ref_id"])
        self.assertEqual(ref["archive_path"], out["archive_path"])
        self.assertEqual(ref["content_sha256"], sha256_text(self.arch.read_text(out["archive_path"])))
        env = build_research_evidence(self.core, "doc/arch", SCOPE, max_chars=50000)
        self.assertStatus(env, Status.OK)
        # repeated paragraph: one hash alias, both sections kept
        n = self.conn.execute("SELECT COUNT(*) FROM source_sections WHERE source_ref_id = ? AND body = '重复段。'",
                              (out["source_ref_id"],)).fetchone()[0]
        self.assertEqual(n, 2)

    def test_sealed_ref_sections_and_aliases_refuse_change(self) -> None:
        out = ingest_document(self.store, doc(TEXT), SCOPE, object_id="doc/arch", archive=self.arch)
        ref = out["source_ref_id"]
        attempts = [
            ("UPDATE source_refs SET content_sha256 = 'x' WHERE source_ref_id = ?", (ref,)),
            ("UPDATE source_refs SET archive_path = NULL WHERE source_ref_id = ?", (ref,)),
            ("DELETE FROM source_refs WHERE source_ref_id = ?", (ref,)),
            ("UPDATE source_sections SET body = '改' WHERE source_ref_id = ?", (ref,)),
            ("DELETE FROM source_sections WHERE source_ref_id = ?", (ref,)),
            ("INSERT OR REPLACE INTO source_sections(source_ref_id, anchor, heading, body, char_count)"
             " SELECT source_ref_id, anchor, heading, '改', 1 FROM source_sections WHERE source_ref_id = ? LIMIT 1",
             (ref,)),
            ("DELETE FROM section_aliases WHERE source_ref_id = ?", (ref,)),
            ("INSERT OR REPLACE INTO source_refs(source_ref_id, source_system, native_id, access_scope)"
             " VALUES (?, 'X', 'Y', 'Z')", (ref,)),
        ]
        for sql, params in attempts:
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.conn.execute(sql, params)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_source_ref(ref, "GOOGLE_DRIVE", "SIMARCH001", "1", None, None, SCOPE)
        # status stays updatable: a sealed source can still become BLOCKED
        self.conn.execute("UPDATE source_refs SET status = 'BLOCKED' WHERE source_ref_id = ?", (ref,))

    def test_unsealed_fixture_refs_stay_editable(self) -> None:
        self.conn.execute("UPDATE source_sections SET body = body || ' ' WHERE source_ref_id = 'src/longdoc-r7'")

    def test_same_version_new_content_keeps_both_and_records_conflict(self) -> None:
        first = ingest_document(self.store, doc(TEXT), SCOPE, object_id="doc/arch", archive=self.arch)
        second = ingest_document(self.store, doc(TEXT + "补一段。\n"), SCOPE, object_id="doc/arch",
                                 archive=self.arch)
        self.assertEqual(second["status"], "INGESTED")
        self.assertNotEqual(first["source_ref_id"], second["source_ref_id"])
        self.assertEqual(second["version_conflict"], [first["source_ref_id"]])
        self.assertIsNotNone(self.store.get_source_ref(first["source_ref_id"]))
        row = self.conn.execute("SELECT candidates FROM conflicts WHERE object_id = 'doc/arch'").fetchone()
        self.assertEqual(len(json.loads(row[0])), 2)
        again = ingest_document(self.store, doc(TEXT + "补一段。\n"), SCOPE, object_id="doc/arch",
                                archive=self.arch)
        self.assertEqual((again["status"], again["source_ref_id"]), ("UNCHANGED", second["source_ref_id"]))

    def test_unchanged_unsealed_revision_is_sealed_on_reingest_with_archive(self) -> None:
        first = ingest_document(self.store, doc(TEXT), SCOPE, object_id="doc/arch")
        self.assertIsNone(first["archive_path"])
        again = ingest_document(self.store, doc(TEXT), SCOPE, object_id="doc/arch", archive=self.arch)
        self.assertEqual(again["status"], "UNCHANGED")
        self.assertTrue(again["sealed_now"])
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM source_sections WHERE source_ref_id = ?", (first["source_ref_id"],))

    def test_local_source_keeps_raw_bytes_and_the_existing_text_convention(self) -> None:
        # Drive exports arrive with a BOM and CRLF. Stores built before the archive hashed
        # Path.read_text(encoding="utf-8-sig") (BOM removed, LF); the text must not change.
        p = Path(tempfile_mkdtemp()) / "a.txt"
        p.write_bytes(("﻿" + TEXT.replace("\n", "\r\n")).encode("utf-8"))
        d = LocalTextSource(p, {"doc_id": "L1", "version": "3"}).fetch()
        self.assertEqual(d.text, p.read_text(encoding="utf-8-sig"))
        self.assertEqual(d.text, TEXT)
        self.assertEqual(d.raw, p.read_bytes())
        rec = self.arch.put(d)
        self.assertEqual(rec.text_sha256, sha256_text(p.read_text(encoding="utf-8-sig")))
        self.assertEqual((self.root / rec.path / "export.txt").read_bytes(), p.read_bytes())
        self.assertEqual(self.arch.verify()["status"], "OK")

    def test_drive_source_lossless_fields_with_fake_http(self) -> None:
        calls = []

        def fake_get(url, token):
            calls.append(url)
            if "/revisions" in url:  # live Drive: revision stamped 85 ms before the file
                return json.dumps({"revisions": [{"id": "r1", "modifiedTime": "2026-11-02T09:39:27.377Z"},
                                                 {"id": "r9", "modifiedTime": "2026-11-07T10:05:36.927Z"}]}).encode()
            if "export?mimeType=text/plain" in url:
                return ("﻿" + TEXT).encode("utf-8")
            if "export?mimeType=" in url:
                return b"PK-docx"
            return json.dumps({"id": "D1", "name": "n", "modifiedTime": "2026-11-07T10:05:37.012Z",
                               "version": "7"}).encode()

        src = DriveSource("D1", ".")
        src._access_token = lambda: "tok"
        src._get = staticmethod(fake_get)
        d = src.fetch()
        self.assertEqual((d.version, d.revision_id, d.text), ("7", "r9", TEXT))
        self.assertEqual([r.filename for r in d.renditions], ["export.docx"])
        self.assertEqual(d.notes, ())  # 85 ms apart is the same edit, not a mismatch
        self.assertTrue(all(u.startswith("https://www.googleapis.com/drive/v3/files/") for u in calls))

        def failing(url, token):
            if "/revisions" in url or "officedocument" in url:
                raise OSError("denied")
            return fake_get(url, token)

        src._get = staticmethod(failing)
        d = src.fetch()  # best effort: still fetches, records why
        self.assertEqual((d.revision_id, d.renditions), (None, ()))
        self.assertEqual(len(d.notes), 2)
