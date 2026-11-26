"""Withhold documents from agents without deleting them, and restore them.

    python -m nexus.withhold --db <db> --scope <scope> --corpus corpus.json withhold doc/<id> [...] --reason "..."
    python -m nexus.withhold --db <db> --scope <scope> --corpus corpus.json restore doc/<id> [...]
    python -m nexus.withhold --db <db> --scope <scope> list

Withholding moves the object (and its source refs) from <scope> to `withheld:<scope>`: every agent
tool is scope-bound, so the document disappears from search and read. Nothing is deleted - the
archive, the sealed source refs and their sections stay as they are (sealed refs allow scope
changes by design). The search index is rebuilt so no stale rows remain. With --corpus, the Drive
id also moves from `include` to `excluded` (with the reason), so a later sync will not fetch it
again; restore moves it back.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .db import Store, connect, init_schema
from .search import ensure_index

PREFIX = "withheld:"


def _native_ids(store: Store, object_id: str) -> list[str]:
    return [r[0] for r in store.conn.execute(
        "SELECT DISTINCT s.native_id FROM aspects a JOIN source_refs s ON s.source_ref_id = a.source_ref_id"
        " WHERE a.object_id = ? AND a.name = 'content'", (object_id,))]


def _move(store: Store, object_id: str, src: str, dst: str) -> bool:
    obj = store.get_object(object_id)
    if obj is None or obj["scope"] != src:
        return False
    refs = [r[0] for r in store.conn.execute(
        "SELECT DISTINCT source_ref_id FROM aspects WHERE object_id = ? AND source_ref_id IS NOT NULL", (object_id,))]
    if store.get_scope(dst) is None:
        store.add_scope(dst, "withheld" if dst.startswith(PREFIX) else "research", "withheld from agents")
    store.conn.execute("UPDATE objects SET scope = ? WHERE object_id = ?", (dst, object_id))
    for ref in refs:
        store.conn.execute("UPDATE source_refs SET access_scope = ? WHERE source_ref_id = ? AND access_scope = ?",
                           (dst, ref, src))
    return True


def _reindex(store: Store) -> None:
    store.conn.execute("DELETE FROM search_meta WHERE key = 'fingerprint'") if store.conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'search_meta'").fetchone() else None
    ensure_index(store.conn)


def withhold(store: Store, scope: str, object_ids: list[str]) -> list[str]:
    done = []
    with store.tx():
        for oid in object_ids:
            if _move(store, oid, scope, PREFIX + scope):
                done.append(oid)
    _reindex(store)
    return done


def restore(store: Store, scope: str, object_ids: list[str]) -> list[str]:
    done = []
    with store.tx():
        for oid in object_ids:
            if _move(store, oid, PREFIX + scope, scope):
                done.append(oid)
    _reindex(store)
    return done


def _update_corpus(path: Path, store: Store, object_ids: list[str], to_excluded: bool, reason: str) -> int:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    ids = {nid for oid in object_ids for nid in _native_ids(store, oid)}
    src, dst = ("include", "excluded") if to_excluded else ("excluded", "include")
    moving = [d for d in corpus.get(src, []) if d["id"] in ids]
    corpus[src] = [d for d in corpus.get(src, []) if d["id"] not in ids]
    for d in moving:
        d = dict(d)
        if to_excluded:
            d["withheld"] = {"reason": reason, "at": time.strftime("%Y-%m-%d")}
        else:
            d.pop("withheld", None)
        corpus.setdefault(dst, []).append(d)
    path.write_text(json.dumps(corpus, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(moving)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.withhold")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--corpus")
    p.add_argument("--reason", default="")
    p.add_argument("action", choices=["withhold", "restore", "list"])
    p.add_argument("objects", nargs="*")
    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    conn = connect(a.db)
    init_schema(conn)
    store = Store(conn)
    if a.action == "list":
        for oid, name in conn.execute("SELECT object_id, display_name FROM objects WHERE scope = ?",
                                      (PREFIX + a.scope,)):
            print(oid, name)
        return 0
    fn = withhold if a.action == "withhold" else restore
    done = fn(store, a.scope, a.objects)
    moved = _update_corpus(Path(a.corpus), store, done, a.action == "withhold", a.reason) if a.corpus else 0
    print(f"{a.action}: {len(done)}/{len(a.objects)} objects; corpus entries moved: {moved}")
    missing = sorted(set(a.objects) - set(done))
    if missing:
        print("not changed (not found in the expected scope):", " ".join(missing))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
