"""CLI: ingest documents (and extracted candidates) into an (isolated) Nexus SQLite database.

Local text export with metadata::

    python -m nexus.ingest --db var/x.db --scope research:demo \
        --text doc.txt --meta meta.json [--object-id doc/brief] [--type document]

A batch from an index file ({name: {doc_id, name, version, ...}}; texts are
<sources-dir>/<name>.txt)::

    python -m nexus.ingest --db var/x.db --scope research:demo --index sources/index.json

Read-only Drive export (token dir holds client_secret.json + token.json)::

    python -m nexus.ingest --db var/x.db --scope research:demo --doc <id> --token-dir <dir>

Add ``--archive <dir>`` to any of these to write each exported revision to the write-once archive
(nexus.archive) first and seal its source_ref. Drive fetches then also try a .docx rendition and
the Docs revision id.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..db import Store, connect, init_schema
from .candidates import import_candidates
from .pipeline import ingest_document
from .sources import DriveSource, LocalTextSource


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.ingest")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--text")
    p.add_argument("--meta")
    p.add_argument("--index")
    p.add_argument("--doc")
    p.add_argument("--token-dir")
    p.add_argument("--object-id")
    p.add_argument("--type", default="document")
    p.add_argument("--candidates", help="JSON payload of extracted entity/relation candidates")
    p.add_argument("--report", help="write the candidate import report (incl. rejections) here")
    p.add_argument("--archive", help="write-once archive root; seals each ingested revision")
    args = p.parse_args(argv)
    archive = None
    if args.archive:
        from ..archive import Archive
        archive = Archive(args.archive)

    conn = connect(args.db)
    init_schema(conn)
    store = Store(conn)
    results = []
    if args.candidates:
        payload = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
        report = import_candidates(store, payload, args.scope)
        if args.report:
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        results.append({"status": report["status"], "counts": report.get("counts"),
                        "rejected_reasons": sorted({r["reason"] for r in report["rejected"]})})
    elif args.index:
        index_path = Path(args.index)
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        for name, meta in index.items():
            src = LocalTextSource(index_path.parent / f"{name}.txt", meta)
            results.append(ingest_document(store, src.fetch(), args.scope,
                                           object_id=f"doc/{name}", object_type=args.type,
                                           archive=archive))
    elif args.text and args.meta:
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
        src = LocalTextSource(args.text, meta)
        results.append(ingest_document(store, src.fetch(), args.scope,
                                       object_id=args.object_id, object_type=args.type,
                                       archive=archive))
    elif args.doc and args.token_dir:
        src = DriveSource(args.doc, args.token_dir)
        results.append(ingest_document(store, src.fetch(), args.scope,
                                       object_id=args.object_id, object_type=args.type,
                                       archive=archive))
    else:
        p.error("give --index, or --text with --meta, or --doc with --token-dir")
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
