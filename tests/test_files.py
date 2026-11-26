"""Universal file store (nexus.files) and its MCP tools.

Covers: any-format storage under SHA-256, provenance of repeated arrivals, PDF pages reaching the
existing search / read / relation surface unchanged, visual rendering of PDF pages and images,
write-once guarantees, the MCP tools (incl. ChatGPT's openai/fileParams declaration), and that the
page marker leaves ordinary document anchors untouched.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

try:
    import pymupdf
    from PIL import Image
    HAVE_DEPS = True
except ImportError:  # pragma: no cover - optional dependencies
    HAVE_DEPS = False

from nexus.agent_tools import AgentTools
from nexus.core import NexusCore
from nexus.db import Store, connect, init_schema
from nexus.files import FileError, FileStore, Source, sniff_mime
from nexus.ingest.sections import split_sections

SCOPE = "research:files-test"


def rmtree_writable(path: Path) -> None:
    """Stored blobs are read-only on purpose; make them writable again to clean up."""
    for p in path.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(path, ignore_errors=True)


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def make_png(size=(800, 600), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@unittest.skipUnless(HAVE_DEPS, "pymupdf and pillow are needed")
class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nexus-files-"))
        self.db_path = str(self.tmp / "t.sqlite")
        self.conn = connect(self.db_path)
        init_schema(self.conn)
        self.store = Store(self.conn)
        self.fs = FileStore(self.store, self.tmp / "files")

    def tearDown(self):
        self.conn.close()

        rmtree_writable(self.tmp)

    def put(self, data, name, source=None):
        return self.fs.put(data, name, SCOPE, "agent:test", source or Source("local", name))

    def test_pdf_pages_are_searchable_and_readable_by_page(self):
        pdf = make_pdf(["Overview of numerical PDEs", "Central difference second derivative",
                        "Crank-Nicolson stability analysis"])
        out = self.put(pdf, "Topic 4 notes.pdf")
        self.assertEqual(out["status"], "OK")
        self.assertEqual((out["mime"], out["pages"], out["readable_as"]), ("application/pdf", 3, "text+visual"))
        tools = AgentTools(NexusCore(self.store), SCOPE)
        hits = tools.nexus_search(query="Crank-Nicolson")["hits"]
        self.assertTrue(hits and hits[0]["object_id"] == out["object_id"])
        self.assertTrue(hits[0]["anchor"].startswith("h3."), hits[0]["anchor"])   # page 3
        texts = [p["text"] for p in tools.nexus_read(object_id=out["object_id"], max_chars=4000)["paragraphs"]]
        self.assertIn("Page 2", texts)
        self.assertIn("Central difference second derivative", texts)

    def test_same_bytes_twice_is_one_file_with_two_sources(self):
        png = make_png()
        first = self.put(png, "a.png", Source("google-drive", "drive-id-1", "https://drive.example/a"))
        second = self.put(png, "renamed.png", Source("chat-upload", "file-abc"))
        self.assertEqual(second["status"], "ALREADY_STORED")
        self.assertEqual(first["object_id"], second["object_id"])
        self.assertEqual(second["name"], "a.png")                      # first name is kept
        self.assertEqual(sorted(s["source_system"] for s in second["sources"]), ["chat-upload", "google-drive"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
        blobs = [p for p in (self.tmp / "files").rglob("*") if p.is_file()]
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0].name, hashlib.sha256(png).hexdigest())

    def test_image_is_visual_only_and_renders_within_limits(self):
        out = self.put(make_png((4000, 3000)), "photo.png")
        self.assertEqual((out["mime"], out["text_chars"], out["readable_as"]), ("image/png", 0, "visual"))
        img, mime, facts = self.fs.render(out["object_id"], max_px=1200)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(facts["original_px"], [4000, 3000])
        self.assertLessEqual(max(facts["sent_px"]), 1200)
        with Image.open(io.BytesIO(img)) as im:
            self.assertLessEqual(max(im.size), 1200)

    def test_pdf_page_render_and_bad_page(self):
        out = self.put(make_pdf(["one", "two"]), "two.pdf")
        img, mime, facts = self.fs.render(out["object_id"], page=2)
        self.assertTrue(img.startswith(b"\x89PNG") and mime == "image/png" and facts["pages"] == 2)
        with self.assertRaises(FileError) as e:
            self.fs.render(out["object_id"], page=3)
        self.assertEqual(e.exception.status, "INVALID_PAGE")

    def test_any_binary_is_stored_with_metadata_only(self):
        data = b"PK\x03\x04" + os.urandom(2048)
        out = self.put(data, "bundle.zip")
        self.assertEqual((out["mime"], out["readable_as"]), ("application/zip", "metadata only"))
        self.assertEqual(self.fs.read_bytes(out["sha256"]), data)
        with self.assertRaises(FileError) as e:
            self.fs.render(out["object_id"])
        self.assertEqual(e.exception.status, "NOT_VISUAL")

    def test_text_file_is_ingested(self):
        out = self.put("Lab notes\nThe Early voltage was 87 V.\n".encode(), "lab.md")
        self.assertEqual(out["mime"], "text/markdown")
        hits = AgentTools(NexusCore(self.store), SCOPE).nexus_search(query="Early voltage")["hits"]
        self.assertEqual(hits[0]["object_id"], out["object_id"])

    def test_file_rows_are_write_once_and_verify_detects_damage(self):
        out = self.put(make_png(), "x.png")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM files")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE files SET bytes = 1")
        self.assertEqual(self.fs.verify()["status"], "OK")
        blob = self.tmp / "files" / out["sha256"][:2] / out["sha256"]
        os.chmod(blob, stat.S_IWRITE | stat.S_IREAD)
        blob.write_bytes(b"tampered")
        report = self.fs.verify()
        self.assertEqual((report["status"], report["corrupt"]), ("FAILED", [out["sha256"]]))
        with self.assertRaises(FileError):
            self.fs.read_bytes(out["sha256"])

    def test_file_object_takes_part_in_relations(self):
        out = self.put(make_pdf(["Numerical PDE finite difference"]), "t4.pdf")
        self.store.add_object("course/DEMF0004/num_pde", "knowledge", "Numerical PDE", SCOPE)
        self.store.add_relation("rel/t", out["object_id"], "covers", "course/DEMF0004/num_pde", "UNVERIFIED",
                                SCOPE, [])
        self.conn.commit()
        rels = self.fs.info(out["object_id"])["relations"]
        self.assertEqual([(r["relation_type"], r["to_id"]) for r in rels], [("covers", "course/DEMF0004/num_pde")])

    def test_sniffing_trusts_bytes_over_names(self):
        self.assertEqual(sniff_mime(b"%PDF-1.7 ...", "looks.png", "image/png"), "application/pdf")
        self.assertEqual(sniff_mime(b"\xff\xd8\xff\xe0..", "a.bin"), "image/jpeg")
        self.assertEqual(sniff_mime(b"\x00\x01\x02", "a.bin"), "application/octet-stream")


class PageMarkerTest(unittest.TestCase):
    def test_marker_makes_page_headings(self):
        _, secs = split_sections("Doc\n⟦page 1⟧\nalpha\n⟦page 12⟧\nbeta\n")
        self.assertEqual([(s.body, s.heading_no, s.ordinal) for s in secs[1:]],
                         [("Page 1", 1, 0), ("alpha", 1, 1), ("Page 12", 12, 0), ("beta", 12, 1)])
        self.assertEqual(secs[-1].heading_path, "Doc > Page 12")

    def test_ordinary_documents_are_unchanged(self):
        text = "标题\n一｜背景\n第一段\n2｜方法\n第二段\n[page 3]\n"
        _, secs = split_sections(text)
        self.assertEqual([s.anchor.split("-")[0] for s in secs], ["h0.1", "h1.0", "h1.1", "h2.0", "h2.1", "h2.2"])


@unittest.skipUnless(HAVE_DEPS, "pymupdf and pillow are needed")
class FileMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nexus-files-mcp-"))
        self.db_path = str(self.tmp / "t.sqlite")
        conn = connect(self.db_path)
        init_schema(conn)
        conn.close()
        self.addCleanup(rmtree_writable, self.tmp)

    def server(self, agent_name="chatgpt"):
        from nexus.mcp_server import build_server
        return build_server(self.db_path, SCOPE, agent_name=agent_name)

    @staticmethod
    def content(server, tool, args):
        res = asyncio.run(server.call_tool(tool, args))
        return getattr(res, "content", None) or (res[0] if isinstance(res, tuple) else res)

    def test_tools_and_file_params_meta(self):
        tools = {t.name: t for t in asyncio.run(self.server().list_tools())}
        for name in ("nexus_file_put", "nexus_file_put_base64", "nexus_file_view", "nexus_file_info"):
            self.assertIn(name, tools)
        self.assertEqual(tools["nexus_file_put"].meta, {"openai/fileParams": ["file"]})
        reader = {t.name for t in asyncio.run(self.server(agent_name=None).list_tools())}
        self.assertIn("nexus_file_view", reader)
        self.assertNotIn("nexus_file_put", reader)            # writes need --agent-name

    def test_put_base64_then_view_returns_an_image(self):
        server = self.server()
        pdf = make_pdf(["Page one text", "Page two text"])
        out = json.loads(self.content(server, "nexus_file_put_base64", {
            "filename": "two.pdf", "data_base64": base64.b64encode(pdf).decode()})[0].text)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["sources"][0]["received_by"], "agent:chatgpt")
        blocks = self.content(server, "nexus_file_view", {"object_id": out["object_id"], "page": 2})
        self.assertEqual([b.type for b in blocks], ["text", "image"])
        self.assertTrue(base64.b64decode(blocks[1].data).startswith(b"\x89PNG"))
        info = json.loads(self.content(server, "nexus_file_info", {"object_id": out["object_id"]})[0].text)
        self.assertEqual(info["pages"], 2)

    def test_put_rejects_unsafe_urls_and_missing_url(self):
        server = self.server()
        for ref, status in [({"file_id": "f1"}, "NO_DOWNLOAD_URL"),
                            ({"download_url": "http://example.com/x"}, "INVALID_URL"),
                            ({"download_url": "https://127.0.0.1/x"}, "INVALID_URL")]:
            out = json.loads(self.content(server, "nexus_file_put", {"file": ref})[0].text)
            self.assertEqual(out["status"], status, ref)

    def test_view_refuses_other_scopes(self):
        conn = connect(self.db_path)
        out = FileStore(Store(conn), self.db_path + ".files").put(make_png(), "x.png", "research:other",
                                                                   "user", Source("local", "x"))
        conn.close()
        blocks = self.content(self.server(), "nexus_file_view", {"object_id": out["object_id"]})
        self.assertEqual(json.loads(blocks[0].text)["status"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
