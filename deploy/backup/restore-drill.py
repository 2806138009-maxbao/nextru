"""Restore drill: rebuild a working Nexus from backups alone, into an empty folder, and check it.

    python -X utf8 deploy/backup/restore-drill.py <repo root> <backups dir> <empty work dir>
        [--files <originals mirror dir>] [--scope <scope>] [--obsidian]

<backups dir> holds main-*.sqlite (local daily backups) or main-*.sqlite.zst (off-site copies),
each with its .manifest.json; the newest is used. --files is the mirror the backup job keeps of
the stored file originals (default: <backups dir>/files when it exists, else ../files).

Steps, each must pass:
  1. take the newest backup (decompress .zst: Python 3.14 compression.zstd, else the zstd CLI)
  2. restore_backup: SHA-256, integrity, row counts and revision heads match the manifest
  3. every file row has its original, and every original matches its SHA-256 (nexus.files verify)
  4. the restored database answers: a native document reads at its current revision, search runs,
     a stored file renders (when the extras are installed)
  5. --obsidian: a fresh vault can be rebuilt from the restored database

Nothing outside <work dir> is written. Exit code 0 only if every step passed.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path


def newest(bk: Path) -> Path:
    found = sorted(list(bk.glob("main-*.sqlite")) + list(bk.glob("main-*.sqlite.zst")),
                   key=lambda p: p.name.removesuffix(".zst"))
    if not found:
        sys.exit(f"no main-*.sqlite[.zst] backup in {bk}")
    return found[-1]


def decompress(z: Path, out: Path) -> None:
    try:
        from compression import zstd  # Python 3.14+
        with zstd.open(z, "rb") as f, open(out, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)
    except ImportError:
        subprocess.run(["zstd", "-q", "-d", "-o", str(out), str(z)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("backups")
    ap.add_argument("work")
    ap.add_argument("--files")
    ap.add_argument("--scope")
    ap.add_argument("--obsidian", action="store_true")
    a = ap.parse_args()
    root, bk, work = Path(a.root), Path(a.backups), Path(a.work)
    sys.path.insert(0, str(root / "src"))
    from nexus.agent_tools import AgentTools
    from nexus.core import NexusCore
    from nexus.db import Store, connect
    from nexus.files import FileStore
    from nexus.native import NativeDocs, restore_backup

    t0 = time.time()
    if work.exists() and any(work.iterdir()):
        sys.exit(f"{work} is not empty")
    work.mkdir(parents=True, exist_ok=True)

    src = newest(bk)
    manifest = src.with_name(src.name.removesuffix(".zst") + ".manifest.json")
    raw = work / src.name.removesuffix(".zst")
    if src.suffix == ".zst":
        decompress(src, raw)
    else:
        shutil.copyfile(src, raw)
    shutil.copy(manifest, work)
    print(f"1. backup {src.name}: {raw.stat().st_size / 1e6:.0f} MB")

    target = work / "restored.sqlite"
    r = restore_backup(str(raw), str(target))
    print("2. restore_backup:", r["status"], "| matches manifest:", r["matches_manifest"])
    assert r["status"] == "OK" and r["matches_manifest"], r

    conn = connect(target)
    files_dir = Path(str(target) + ".files")
    mirror = Path(a.files) if a.files else next((p for p in (bk / "files", bk.parent / "files") if p.is_dir()), None)
    n_files = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'files'").fetchone()[0] and \
        conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    if mirror is not None:
        shutil.copytree(mirror, files_dir)
    fs = FileStore(Store(conn), files_dir)
    v = fs.verify() if n_files else {"status": "OK", "files": 0, "missing": [], "corrupt": []}
    print(f"3. stored originals: {v['files']} in the DB, mirror {mirror or 'none'};"
          f" missing {len(v['missing'])}, corrupt {len(v['corrupt'])} -> {v['status']}")
    assert v["status"] == "OK", v

    scope = a.scope or conn.execute(
        "SELECT scope FROM objects GROUP BY scope ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    doc = conn.execute("SELECT doc_id FROM native_documents WHERE archived_at IS NULL AND scope = ?"
                       " ORDER BY updated_at DESC LIMIT 1", (scope,)).fetchone()
    if doc:
        d = NativeDocs(conn).read(doc[0])
        print(f"4a. newest native document reads at revision {d['revision']} ({len(d['nodes'])} nodes)")
    # search for a word the restored data itself contains: the drill fails if nothing comes back
    import re
    name = conn.execute("SELECT display_name FROM objects WHERE scope = ? AND display_name IS NOT NULL"
                        " ORDER BY rowid DESC LIMIT 1", (scope,)).fetchone()[0]
    word = max(re.findall(r"[A-Za-z]{4,}|[一-鿿]{2,}", name) or [name], key=len)[:12]
    hits = AgentTools(NexusCore(Store(conn)), scope).nexus_search(query=word)
    print(f"4b. search {word!r} in {scope}: {hits.get('status')}, {len(hits.get('hits') or [])} hits")
    assert hits.get("status") == "OK" and hits.get("hits"), hits
    if n_files:
        visual = conn.execute("SELECT object_id FROM files WHERE mime = 'application/pdf'"
                              " OR mime LIKE 'image/%' LIMIT 1").fetchone()
        if visual:
            try:
                img, mime, _ = fs.render(visual[0])
                print(f"4c. {visual[0]} renders: {mime}, {len(img)} bytes")
            except ImportError:
                print("4c. render NOT_RUN (pymupdf / pillow not installed)")

    if a.obsidian:
        from nexus import obsidian as ob
        ob.log = lambda *x, **k: None
        st = ob.Bridge(conn, scope, work / "vault", work / "state.json").sync_once(full=True)
        print("5. fresh Obsidian vault rebuilt:", st["created_here"], "notes")
    print(f"restore drill OK in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
