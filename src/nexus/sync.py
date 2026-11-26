"""On-demand sync of a managed list of Google Docs into a Nexus store (read-only against Drive).

    python -m nexus.sync --db <db> --scope <scope> --token-dir <gdrive-mcp> --archive <root> \
        --corpus corpus.json [--jobs 4] [--report report.json] [--dry-run]

corpus.json: {"include": [{"id": "<drive id>", "name": "..."}, ...]} - only these documents are
touched; nothing else in Drive is read. For each document Drive is asked for its version only;
unchanged documents cost one metadata request. New or changed documents are exported (text, .docx,
revision id), archived write-once and ingested as a new sealed revision; earlier revisions stay.
Fetches run in parallel; all store writes happen on this thread. The full-text index is rebuilt at
the end so the first agent search after a sync is not slow.

Not a background job: run it when you want the store brought up to date.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from .archive import Archive
from .db import Store, connect, init_schema
from .ingest import ingest_document
from .ingest.sources import DriveSource
from .search import ensure_index


def stored_versions(store: Store) -> dict[str, int]:
    out: dict[str, int] = {}
    for native_id, rev in store.conn.execute(
            "SELECT native_id, revision FROM source_refs WHERE source_system = 'GOOGLE_DRIVE'"):
        try:
            out[native_id] = max(out.get(native_id, 0), int(rev))
        except (TypeError, ValueError):
            continue
    return out


def object_for(store: Store, native_id: str) -> str | None:
    row = store.conn.execute(
        "SELECT a.object_id FROM aspects a JOIN source_refs s ON s.source_ref_id = a.source_ref_id"
        " WHERE a.name = 'content' AND s.native_id = ? ORDER BY a.revision DESC LIMIT 1", (native_id,)).fetchone()
    return row[0] if row else None


class _Drive:
    def __init__(self, token_dir: str):
        self.token_dir = token_dir
        self._src = DriveSource("x", token_dir)
        self._token = (self._src._access_token(), time.monotonic())

    def token(self) -> str:
        if time.monotonic() - self._token[1] > 45 * 60:
            self._token = (self._src._access_token(), time.monotonic())
        return self._token[0]

    def version(self, doc_id: str) -> dict:
        req = urllib.request.Request(f"{DriveSource.API}{doc_id}?fields=version,modifiedTime,trashed",
                                     headers={"Authorization": f"Bearer {self.token()}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())

    def fetch(self, doc_id: str):
        src = DriveSource(doc_id, self.token_dir)
        src._access_token = self.token  # reuse one token for the whole run
        return src.fetch()


def sync(db: str, scope: str, token_dir: str, archive_root: str, corpus: list[dict], jobs: int = 4,
         dry_run: bool = False, log=print, drive: Any = None) -> dict[str, Any]:
    conn = connect(db)
    init_schema(conn)
    store = Store(conn)
    have = stored_versions(store)
    drive = drive or _Drive(token_dir)
    archive = Archive(archive_root)
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "unchanged": 0,
                              "new": [], "changed": [], "errors": [], "trashed": []}
    todo = []
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        metas = dict(zip([d["id"] for d in corpus], ex.map(lambda d: _safe(drive.version, d["id"]), corpus)))
    for d in corpus:
        meta = metas[d["id"]]
        if isinstance(meta, Exception):
            report["errors"].append({"id": d["id"], "name": d.get("name"), "stage": "version",
                                     "error": f"{type(meta).__name__}: {meta}"[:200]})
            continue
        if meta.get("trashed"):
            report["trashed"].append({"id": d["id"], "name": d.get("name")})
            continue
        if have.get(d["id"]) == int(meta["version"]):
            report["unchanged"] += 1
            continue
        todo.append((d, "changed" if d["id"] in have else "new", meta["version"]))
    log(f"checked {len(corpus)}: {report['unchanged']} unchanged, {len(todo)} to fetch, "
        f"{len(report['errors'])} errors")
    if dry_run:
        report["would_fetch"] = [{"id": d["id"], "name": d.get("name"), "kind": k, "drive_version": v}
                                 for d, k, v in todo]
        return report
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        futures = {ex.submit(_safe, drive.fetch, d["id"]): (d, kind) for d, kind, _v in todo}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            d, kind = futures[fut]
            doc = fut.result()
            if isinstance(doc, Exception):
                report["errors"].append({"id": d["id"], "name": d.get("name"), "stage": "fetch",
                                         "error": f"{type(doc).__name__}: {doc}"[:200]})
                continue
            try:
                res = ingest_document(store, doc, scope, object_id=object_for(store, d["id"]), archive=archive)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"id": d["id"], "name": d.get("name"), "stage": "ingest",
                                         "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            report[kind].append({"id": d["id"], "name": doc.name, "version": doc.version,
                                 "sections": res.get("sections"), "status": res["status"],
                                 "notes": list(doc.notes)})
            if i % 25 == 0:
                log(f"  {i}/{len(todo)} fetched")
    ensure_index(conn)
    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log(f"new {len(report['new'])}, changed {len(report['changed'])}, unchanged {report['unchanged']}, "
        f"trashed {len(report['trashed'])}, errors {len(report['errors'])}")
    return report


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - reported per document
        return exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.sync")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--token-dir", required=True)
    p.add_argument("--archive", required=True)
    p.add_argument("--corpus", required=True, help='JSON {"include": [{"id", "name"}, ...]}')
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--report")
    p.add_argument("--dry-run", action="store_true", help="only check versions; fetch nothing")
    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    corpus = json.loads(Path(a.corpus).read_text(encoding="utf-8"))["include"]
    rep = sync(a.db, a.scope, a.token_dir, a.archive, corpus, jobs=max(1, min(a.jobs, 8)), dry_run=a.dry_run)
    if a.report:
        Path(a.report).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if not rep["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
