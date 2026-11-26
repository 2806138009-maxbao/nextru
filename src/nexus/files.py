"""Universal file store: any file in, original bytes kept, text and pages made usable.

What ``FileStore.put`` does with a file:

* the bytes go to ``<files-dir>/<sha[:2]>/<sha>``: content-addressed, written once, then read-only.
  The same bytes arriving again (another name, another source) write nothing new;
* SQLite records the file (``files``), every place it came from (``file_sources``) and a normal
  object ``file/<sha16>`` (type ``file``). Because it is an ordinary object, ``nexus_add_relation``
  links it to courses, topics or documents, and ``nexus_related`` / ``nexus_trace`` see it;
* text it contains is ingested with the standard pipeline (``ingest_document``). PDF pages become
  headings ``Page n``, so search hits and reads point at a page. Text files are ingested as is;
* images, scans without a text layer and other binaries get metadata only. Reading them is visual:
  ``render`` turns a PDF page or an image into a PNG/JPEG for a vision-capable model.

Nothing here writes to Google Drive or any other source. PDF and image handling need PyMuPDF and
Pillow (``pip install pymupdf pillow``); without them a file is still stored, and the result says
which step was not run.

CLI::

    python -m nexus.files --db var/x.sqlite --scope research:demo put lecture.pdf
    python -m nexus.files --db ... --scope ... drive <drive-file-id> --token-dir <dir>
    python -m nexus.files --db ... --scope ... render file/0123abcd --page 3 --out p3.png
    python -m nexus.files --db ... --scope ... list
    python -m nexus.files --db ... --scope ... verify
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Store, connect, init_schema, utcnow

SOURCE_SYSTEM = "nexus-file"
MAX_RENDER_PX = 1600            # long side of a rendered page / image sent to a model
MAX_RENDER_BYTES = 1_500_000    # shrink until the encoded image is below this
TEXT_MIMES = ("text/plain", "text/markdown", "text/csv")
TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml")
SIGNATURES = [(b"%PDF-", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "image/png"),
              (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")]


class FileError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status

    def as_dict(self) -> dict:
        return {"status": self.status, "error": str(self)}


def sniff_mime(data: bytes, name: str = "", declared: str | None = None) -> str:
    for sig, mime in SIGNATURES:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"PK\x03\x04":
        return declared or "application/zip"
    lower = name.lower()
    if lower.endswith(TEXT_SUFFIXES) or (declared or "").startswith("text/"):
        try:
            data.decode("utf-8")
            return "text/markdown" if lower.endswith((".md", ".markdown")) else (
                "text/csv" if lower.endswith((".csv", ".tsv")) else "text/plain")
        except UnicodeDecodeError:
            pass
    return declared or "application/octet-stream"


def file_object_id(sha256: str) -> str:
    return f"file/{sha256[:16]}"


@dataclass(frozen=True)
class Source:
    """Where the bytes came from (one ``file_sources`` row)."""
    system: str                  # google-drive | chat-upload | local | base64
    native_id: str
    location: str | None = None


def pdf_pages(data: bytes) -> list[str]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [page.get_text("text") for page in doc]


def paged_text(name: str, pages: list[str]) -> str:
    """Ingestible text: title line, then ``⟦page n⟧`` before each page's lines (see sections.py)."""
    out = [name]
    for n, text in enumerate(pages, start=1):
        out.append(f"⟦page {n}⟧")
        out.extend(line for line in text.splitlines() if line.strip())
    return "\n".join(out) + "\n"


class FileStore:
    def __init__(self, store: Store, files_dir: str | Path):
        self.store = store
        self.conn = store.conn
        self.root = Path(files_dir)

    # ---- bytes -------------------------------------------------------------------------------
    def _blob(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def _write_blob(self, sha: str, data: bytes) -> str:
        path = self._blob(sha)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise FileError("CORRUPT", f"stored blob {sha[:12]} does not match its hash")
            return path.relative_to(self.root).as_posix()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{sha}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP)
        return path.relative_to(self.root).as_posix()

    def read_bytes(self, sha: str) -> bytes:
        data = self._blob(sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise FileError("CORRUPT", f"stored blob {sha[:12]} does not match its hash")
        return data

    # ---- put ---------------------------------------------------------------------------------
    def put(self, data: bytes, name: str, scope: str, actor: str, source: Source,
            declared_mime: str | None = None) -> dict[str, Any]:
        if not data:
            raise FileError("EMPTY", "file has no bytes")
        sha = hashlib.sha256(data).hexdigest()
        object_id = file_object_id(sha)
        now = utcnow()
        existing = self.get(sha)
        blob_path = self._write_blob(sha, data)
        notes: list[str] = []

        if existing is None:
            mime = sniff_mime(data, name, declared_mime)
            pages: list[str] | None = None
            text = ""
            if mime == "application/pdf":
                try:
                    pages = pdf_pages(data)
                    text = paged_text(name, pages) if any(p.strip() for p in pages) else ""
                    if not text:
                        notes.append("PDF has no text layer (scan?): stored, readable visually only")
                except ImportError:
                    notes.append("PyMuPDF not installed: PDF stored, pages not extracted (NOT_RUN)")
                except Exception as e:          # damaged PDF: keep the bytes, report the failure
                    notes.append(f"PDF text extraction failed: {type(e).__name__}: {e}")
            elif mime in TEXT_MIMES:
                text = data.decode("utf-8")
                if not text.startswith(name):
                    text = f"{name}\n{text}"

            if text:
                from .ingest.pipeline import ingest_document
                from .ingest.sources import FetchedDocument
                ingest_document(self.store, FetchedDocument(
                    SOURCE_SYSTEM, sha, name, "1", None, source.location, text),
                    scope, object_id=object_id, object_type="file", display_name=name)
            with self.store.tx():
                if self.store.get_scope(scope) is None:
                    self.store.add_scope(scope, "research", "created by nexus.files")
                if self.store.get_object(object_id) is None:
                    self.store.add_object(object_id, "file", name, scope, now)
                self.conn.execute(
                    "INSERT INTO files(sha256, object_id, bytes, mime, name, pages, text_chars, blob_path,"
                    " created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sha, object_id, len(data), mime, name, len(pages) if pages is not None else None,
                     len(text), blob_path, now, actor))
                self._add_source(sha, source, name, now, actor)
            status = "OK"
        else:
            with self.store.tx():
                self._add_source(sha, source, name, now, actor)
            status = "ALREADY_STORED"
        out = {"status": status, **self.info(object_id)}
        if notes:
            out["notes"] = notes
        return out

    def _add_source(self, sha: str, source: Source, name: str, now: str, actor: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO file_sources(sha256, source_system, native_id, name, location,"
            " received_at, received_by) VALUES (?,?,?,?,?,?,?)",
            (sha, source.system, source.native_id, name, source.location, now, actor))

    # ---- lookups -----------------------------------------------------------------------------
    def get(self, sha_or_object_id: str) -> dict | None:
        key = sha_or_object_id
        row = self.conn.execute(
            "SELECT * FROM files WHERE sha256 = ? OR object_id = ? OR sha256 LIKE ? LIMIT 2",
            (key, key, (key + "%") if len(key) >= 12 else "\0")).fetchall()
        return dict(row[0]) if len(row) == 1 else None

    def info(self, sha_or_object_id: str) -> dict[str, Any]:
        f = self.get(sha_or_object_id)
        if f is None:
            raise FileError("NOT_FOUND", f"no stored file {sha_or_object_id!r}")
        sources = [dict(r) for r in self.conn.execute(
            "SELECT source_system, native_id, name, location, received_at, received_by FROM file_sources"
            " WHERE sha256 = ? ORDER BY received_at", (f["sha256"],))]
        obj = self.store.get_object(f["object_id"]) or {}
        relations = [dict(r) for r in self.conn.execute(
            "SELECT relation_id, from_id, relation_type, to_id, verification FROM relations"
            " WHERE (from_id = ? OR to_id = ?) AND expired_at IS NULL ORDER BY created_at",
            (f["object_id"], f["object_id"]))]
        readable = ("text" if f["text_chars"] else "") + (
            "+visual" if f["mime"] == "application/pdf" or f["mime"].startswith("image/") else "")
        return {"object_id": f["object_id"], "sha256": f["sha256"], "name": f["name"], "mime": f["mime"],
                "bytes": f["bytes"], "pages": f["pages"], "text_chars": f["text_chars"],
                "readable_as": readable.strip("+") or "metadata only",
                "scope": obj.get("scope"), "created_at": f["created_at"], "created_by": f["created_by"],
                "sources": sources, "relations": relations}

    def list(self, scope: str | None = None, limit: int = 50) -> list[dict]:
        sql = ("SELECT f.object_id, f.name, f.mime, f.bytes, f.pages, f.text_chars, f.created_at FROM files f"
               " JOIN objects o ON o.object_id = f.object_id")
        args: tuple = ()
        if scope:
            sql += " WHERE o.scope = ?"
            args = (scope,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY f.created_at DESC LIMIT ?", (*args, limit))]

    # ---- visual ------------------------------------------------------------------------------
    def render(self, sha_or_object_id: str, page: int = 1, max_px: int = MAX_RENDER_PX) -> tuple[bytes, str, dict]:
        """(image bytes, mime, facts) for a PDF page or an image, sized for a vision model."""
        f = self.get(sha_or_object_id)
        if f is None:
            raise FileError("NOT_FOUND", f"no stored file {sha_or_object_id!r}")
        data = self.read_bytes(f["sha256"])
        max_px = max(200, min(int(max_px), 3000))
        if f["mime"] == "application/pdf":
            import pymupdf
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if not 1 <= page <= doc.page_count:
                    raise FileError("INVALID_PAGE", f"page {page} outside 1..{doc.page_count}")
                p = doc[page - 1]
                while True:
                    zoom = max_px / max(p.rect.width, p.rect.height)
                    png = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
                    if len(png) <= MAX_RENDER_BYTES or max_px <= 400:
                        break
                    max_px = int(max_px * 0.75)
                return png, "image/png", {"page": page, "pages": doc.page_count, "long_side_px": max_px}
        if f["mime"].startswith("image/"):
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                orig = im.size
                im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                while True:
                    copy = im.copy()
                    copy.thumbnail((max_px, max_px))
                    buf = io.BytesIO()
                    if copy.mode == "RGBA":
                        copy.save(buf, "PNG", optimize=True)
                        mime = "image/png"
                    else:
                        copy.save(buf, "JPEG", quality=85)
                        mime = "image/jpeg"
                    if buf.tell() <= MAX_RENDER_BYTES or max_px <= 400:
                        break
                    max_px = int(max_px * 0.75)
                return buf.getvalue(), mime, {"original_px": list(orig), "sent_px": list(copy.size)}
        raise FileError("NOT_VISUAL", f"{f['mime']} has no visual rendering; read its text or metadata")

    # ---- integrity ---------------------------------------------------------------------------
    def verify(self) -> dict[str, Any]:
        bad, missing, n = [], [], 0
        for row in self.conn.execute("SELECT sha256, blob_path FROM files"):
            n += 1
            path = self.root / row["blob_path"]
            if not path.exists():
                missing.append(row["sha256"])
            elif hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                bad.append(row["sha256"])
        return {"status": "OK" if not bad and not missing else "FAILED", "files": n,
                "missing": missing, "corrupt": bad}


def default_files_dir(db_path: str | Path) -> Path:
    """``<db>.files`` next to the database (inside the same backed-up, writable directory)."""
    return Path(str(db_path) + ".files")


MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def fetch_url(url: str, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Download a chat attachment (ChatGPT passes a short-lived https download_url).

    https only, and the host must resolve to public addresses: the server never fetches from its
    own network on an agent's say-so (SSRF). Redirects are re-checked the same way."""
    import ipaddress
    import socket
    import urllib.error
    import urllib.parse
    import urllib.request

    def check(u: str) -> None:
        parts = urllib.parse.urlsplit(u)
        if parts.scheme != "https" or not parts.hostname:
            raise FileError("INVALID_URL", "download_url must be an https URL")
        for info in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise FileError("INVALID_URL", "download_url resolves to a non-public address")

    class Checked(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    check(url)
    opener = urllib.request.build_opener(Checked)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "nexus-files/1"}), timeout=60) as r:
            data = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:     # e.g. 403 on an expired chat attachment link
        raise FileError("DOWNLOAD_FAILED", f"download_url answered HTTP {e.code}; the link may have "
                        "expired: attach the file again") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FileError("DOWNLOAD_FAILED", f"could not download the file: {type(e).__name__}") from None
    if len(data) > max_bytes:
        raise FileError("TOO_LARGE", f"file is larger than {max_bytes // (1024 * 1024)} MB")
    return data


def drive_download(file_id: str, token_dir: str) -> tuple[bytes, dict]:
    """Read-only Drive download (bytes + metadata), using the ingest DriveSource credentials."""
    import urllib.parse
    from .ingest.sources import DriveSource
    src = DriveSource(file_id, token_dir)
    token = src._access_token()
    meta = json.loads(src._get(f"{src.API}{file_id}?supportsAllDrives=true&fields="
                               + urllib.parse.quote("id,name,mimeType,size,webViewLink,md5Checksum"), token))
    if meta.get("mimeType", "").startswith("application/vnd.google-apps."):
        raise FileError("NATIVE_GOOGLE_FILE", "Google Docs/Sheets are ingested by nexus.ingest, not stored as files")
    data = src._get(f"{src.API}{file_id}?alt=media&supportsAllDrives=true", token)
    data = data if isinstance(data, bytes) else data.encode("utf-8")
    if meta.get("md5Checksum") and hashlib.md5(data).hexdigest() != meta["md5Checksum"]:
        raise FileError("CORRUPT", "downloaded bytes do not match Drive's md5Checksum")
    return data, meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.files")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--files-dir", help="default: <db>.files")
    p.add_argument("--actor", default="user:cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    put = sub.add_parser("put")
    put.add_argument("path")
    put.add_argument("--name")
    drv = sub.add_parser("drive")
    drv.add_argument("file_id")
    drv.add_argument("--token-dir", required=True)
    ren = sub.add_parser("render")
    ren.add_argument("object_id")
    ren.add_argument("--page", type=int, default=1)
    ren.add_argument("--max-px", type=int, default=MAX_RENDER_PX)
    ren.add_argument("--out", required=True)
    inf = sub.add_parser("info")
    inf.add_argument("object_id")
    sub.add_parser("list")
    sub.add_parser("verify")
    a = p.parse_args(argv)

    conn = connect(a.db)
    init_schema(conn)
    fs = FileStore(Store(conn), a.files_dir or default_files_dir(a.db))
    try:
        if a.cmd == "put":
            path = Path(a.path)
            out = fs.put(path.read_bytes(), a.name or path.name, a.scope, a.actor,
                         Source("local", path.name))
        elif a.cmd == "drive":
            data, meta = drive_download(a.file_id, a.token_dir)
            out = fs.put(data, meta["name"], a.scope, a.actor,
                         Source("google-drive", meta["id"], meta.get("webViewLink")), meta.get("mimeType"))
        elif a.cmd == "render":
            img, mime, facts = fs.render(a.object_id, a.page, a.max_px)
            Path(a.out).write_bytes(img)
            out = {"status": "OK", "mime": mime, "bytes": len(img), **facts, "out": a.out}
        elif a.cmd == "info":
            out = fs.info(a.object_id)
        elif a.cmd == "list":
            out = {"status": "OK", "files": fs.list(a.scope)}
        else:
            out = fs.verify()
    except FileError as e:
        out = e.as_dict()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("status") in ("OK", "ALREADY_STORED", None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
