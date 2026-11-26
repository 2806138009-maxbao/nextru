"""Write-once document archive: the exported original of every ingested revision, with hashes.

Layout under ``root``::

    manifest.jsonl                                   append-only, one line per archive event
    <source_system>/<doc_id>/v<version>__<sha12>/    one directory per distinct exported content
        export.txt                                   plain-text export, bytes exactly as received
        export.docx                                  lossless rendition when the source gave one
        meta.json                                    identity, hashes, notes

``sha12`` is the first 12 hex chars of the raw export's SHA-256, so the same version label with
different bytes gets a *second* directory instead of replacing the first; both are flagged
``version_conflict`` in the manifest. Writing is staged in a temporary directory and renamed into
place, then every file is set read-only. Archiving bytes that are already archived is a no-op.

Two hashes are recorded and kept apart on purpose:

* ``files[*].sha256``  hash of the stored bytes (what ``verify`` recomputes)
* ``text_sha256``      ``db.sha256_text`` of ``decode_export(bytes)`` (BOM removed, LF) — the value Nexus
                       stores in ``source_refs.content_sha256``; ``verify`` recomputes it too

Limits: read-only file attributes and append-only conventions stop accidents, not a determined
local user. Tamper *evidence* comes from ``verify`` against the manifest; keep a copy of the
manifest (or the whole root) somewhere else if that matters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import sha256_text, utcnow
from .ingest.sources import FetchedDocument, LocalTextSource, decode_export

ARCHIVE_FORMAT = "nexus-archive/1"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "_"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ArchiveRecord:
    path: str             # relative to the archive root, POSIX separators
    raw_sha256: str
    text_sha256: str
    created: bool         # False when the same bytes were already archived
    version_conflict: bool


class ArchiveError(RuntimeError):
    pass


class Archive:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # -- writing ---------------------------------------------------------------------
    def put(self, doc: FetchedDocument) -> ArchiveRecord:
        raw = doc.raw if doc.raw is not None else doc.text.encode("utf-8")
        raw_sha, text_sha = _sha(raw), sha256_text(doc.text)
        if decode_export(raw) != doc.text:
            raise ArchiveError("raw export does not decode to the document text; refusing to archive")
        base = self.root / _slug(doc.source_system) / _slug(doc.doc_id)
        rel = f"{_slug(doc.source_system)}/{_slug(doc.doc_id)}/v{_slug(doc.version)}__{raw_sha[:12]}"
        final = self.root / rel
        siblings = sorted(p.name for p in base.glob(f"v{_slug(doc.version)}__*") if p.name != final.name) \
            if base.exists() else []
        if final.exists():
            problems = self._verify_dir(final)
            if problems:
                raise ArchiveError(f"existing archive entry {rel} fails verification: {problems}")
            return ArchiveRecord(rel, raw_sha, text_sha, False, bool(siblings))

        files = {"export.txt": ("text/plain; charset=utf-8", raw)}
        for r in doc.renditions:
            if r.filename in files or r.filename == "meta.json" or "/" in r.filename or "\\" in r.filename:
                raise ArchiveError(f"bad rendition filename {r.filename!r}")
            files[r.filename] = (r.mime, r.data)
        meta = {
            "format": ARCHIVE_FORMAT, "archived_at": utcnow(),
            "source_system": doc.source_system, "doc_id": doc.doc_id, "name": doc.name,
            "version": doc.version, "revision_id": doc.revision_id,
            "modified_time": doc.modified_time, "web_view": doc.web_view,
            "text_sha256": text_sha, "text_chars": len(doc.text),
            "files": {n: {"sha256": _sha(d), "bytes": len(d), "mime": m} for n, (m, d) in files.items()},
            "version_conflict_with": siblings, "notes": list(doc.notes),
        }
        base.mkdir(parents=True, exist_ok=True)
        # not tempfile.mkdtemp: its 0700 ACL directory cannot always be re-opened on Windows
        stage = base / f".stage-{uuid.uuid4().hex}"
        stage.mkdir()
        try:
            for name, (_, data) in files.items():
                (stage / name).write_bytes(data)
            (stage / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            for p in stage.iterdir():
                os.chmod(p, stat.S_IREAD)
            os.replace(stage, final)
        except FileExistsError:  # a concurrent writer won the race with the same bytes
            shutil.rmtree(stage, onerror=_force_remove)
            return ArchiveRecord(rel, raw_sha, text_sha, False, bool(siblings))
        except BaseException:
            shutil.rmtree(stage, onerror=_force_remove)
            raise
        self._append_manifest({"event": "archived", "path": rel, "at": meta["archived_at"],
                               "source_system": doc.source_system, "doc_id": doc.doc_id,
                               "version": doc.version, "revision_id": doc.revision_id,
                               "raw_sha256": raw_sha, "text_sha256": text_sha,
                               "files": {n: v["sha256"] for n, v in meta["files"].items()},
                               "version_conflict_with": siblings})
        return ArchiveRecord(rel, raw_sha, text_sha, True, bool(siblings))

    def _append_manifest(self, row: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "manifest.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # -- reading / checking ----------------------------------------------------------
    def read_text(self, rel: str) -> str:
        return decode_export((self.root / rel / "export.txt").read_bytes())

    def _verify_dir(self, d: Path) -> list[str]:
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return [f"meta.json unreadable: {type(exc).__name__}"]
        problems = []
        for name, info in meta["files"].items():
            p = d / name
            if not p.exists():
                problems.append(f"{name} missing")
                continue
            data = p.read_bytes()
            if _sha(data) != info["sha256"] or len(data) != info["bytes"]:
                problems.append(f"{name} hash mismatch")
            if os.access(p, os.W_OK):
                problems.append(f"{name} is writable")
        if "export.txt" in meta["files"] and (d / "export.txt").exists():
            if sha256_text(decode_export((d / "export.txt").read_bytes())) != meta["text_sha256"]:
                problems.append("text_sha256 mismatch")
        if not d.name.endswith("__" + meta["files"]["export.txt"]["sha256"][:12]):
            problems.append("directory name does not match export hash")
        return problems

    def verify(self) -> dict[str, Any]:
        """Recompute every hash, and check the manifest and the directories agree."""
        rows = []
        mf = self.root / "manifest.jsonl"
        if mf.exists():
            rows = [json.loads(line) for line in mf.read_text(encoding="utf-8").splitlines() if line.strip()]
        listed = {r["path"]: r for r in rows if r.get("event") == "archived"}
        on_disk = {p.parent.relative_to(self.root).as_posix() for p in self.root.glob("*/*/v*/meta.json")}
        report = {"entries": len(on_disk), "manifest_rows": len(rows), "ok": [], "problems": {}}
        for rel in sorted(on_disk | set(listed)):
            probs = []
            if rel not in on_disk:
                probs.append("in manifest but missing on disk")
            else:
                probs += self._verify_dir(self.root / rel)
                if rel not in listed:
                    probs.append("on disk but not in manifest")
                else:
                    meta = json.loads((self.root / rel / "meta.json").read_text(encoding="utf-8"))
                    if listed[rel]["raw_sha256"] != meta["files"]["export.txt"]["sha256"]:
                        probs.append("manifest hash differs from meta.json")
            if probs:
                report["problems"][rel] = probs
            else:
                report["ok"].append(rel)
        report["status"] = "OK" if not report["problems"] else "PROBLEMS"
        return report


def _force_remove(func, path, _exc):  # read-only files inside a failed stage dir
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.archive")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="archive local plain-text exports listed in an index file")
    a.add_argument("--root", required=True)
    a.add_argument("--index", required=True, help="{name: {doc_id, name, version, ...}}; texts are <dir>/<name>.txt")
    v = sub.add_parser("verify", help="recompute every hash under the archive root")
    v.add_argument("--root", required=True)
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    arch = Archive(args.root)
    if args.cmd == "add":
        index_path = Path(args.index)
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        out = []
        for name, meta in index.items():
            rec = arch.put(LocalTextSource(index_path.parent / f"{name}.txt", meta).fetch())
            out.append({"name": name, **rec.__dict__})
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    report = arch.verify()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
