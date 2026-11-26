"""Embedding service for semantic recall (see ``semantic.py``).

    python -m nexus.semantic_service --db main.sqlite --vectors vectors.sqlite \
        [--backend st --model BAAI/bge-m3] [--host 127.0.0.1 --port 8767]

* Reads the Nexus database read-only; never writes to it. Vectors live in their own SQLite file,
  keyed by chunk hash, so a restart or model swap never touches Nexus data and a backfill resumes
  where it stopped.
* Every ``--interval`` seconds it checks the full-text index fingerprint; on change it rebuilds the
  chunk list, embeds the chunks it has no vector for, and drops vectors no chunk uses any more.
  Searches during a backfill use the vectors that exist and report ``coverage``.
* HTTP (loopback only): ``POST /search {query, scope, kinds, k}`` and ``GET /health``.

Backends: ``st`` = sentence-transformers (needs ``pip install sentence-transformers``);
``hash`` = a dependency-free character-bigram hashing embedder for tests and dry runs.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .semantic import as_hit, build_chunks, index_fingerprint

log = logging.getLogger("nexus.semantic")
SNIPPET = 140

try:  # numpy is optional: the hash backend and the tests run without it
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


# ------------------------------------------------------------------ embedders
class HashEmbedder:
    """Character unigram+bigram feature hashing. Not semantic, but deterministic and fast."""

    name = "hash-256"
    dim = 256

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            t = (t or "").lower()
            feats = list(t) + [t[i:i + 2] for i in range(len(t) - 1)]
            for f in feats:
                if f.strip():
                    h = int.from_bytes(hashlib.md5(f.encode()).digest()[:4], "little")
                    v[h % self.dim] += 1.0 if len(f) == 1 else 2.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class STEmbedder:
    def __init__(self, model: str, max_seq: int, threads: int | None):
        import torch
        from sentence_transformers import SentenceTransformer
        if threads:
            torch.set_num_threads(threads)
        self.model = SentenceTransformer(model, device="cpu")
        self.model.max_seq_length = max_seq
        self.name = f"{model}@{max_seq}"
        dim = (getattr(self.model, "get_embedding_dimension", None)
               or self.model.get_sentence_embedding_dimension)
        self.dim = dim()

    def encode(self, texts: list[str]):
        return self.model.encode(texts, batch_size=16, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False)


# ------------------------------------------------------------------ vector store
def _pack(vec) -> bytes:
    if _np is not None:
        return _np.asarray(vec, dtype=_np.float16).tobytes()
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes, dim: int):
    if _np is not None:
        return _np.frombuffer(blob, dtype=_np.float16).astype(_np.float32) if len(blob) == dim * 2 \
            else _np.frombuffer(blob, dtype=_np.float32)
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


class VectorStore:
    def __init__(self, path: str, model_name: str, dim: int):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, data BLOB NOT NULL);")
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        if row and row[0] != f"{model_name}/{dim}":
            log.warning("model changed (%s -> %s/%d): dropping all vectors", row[0], model_name, dim)
            self.conn.execute("DELETE FROM vec")
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (f"{model_name}/{dim}",))
        self.conn.commit()
        self.dim = dim

    def keys(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT key FROM vec")}

    def put(self, items: list[tuple[str, object]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?)",
                              [(k, _pack(v)) for k, v in items])
        self.conn.commit()

    def drop(self, keys: set[str]) -> None:
        self.conn.executemany("DELETE FROM vec WHERE key = ?", [(k,) for k in keys])
        self.conn.commit()

    def load(self, keys: list[str]) -> dict:
        out = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            q = "SELECT key, data FROM vec WHERE key IN (%s)" % ",".join("?" * len(part))
            for k, blob in self.conn.execute(q, part):
                out[k] = _unpack(blob, self.dim)
        return out


# ------------------------------------------------------------------ service
class SemanticIndex:
    def __init__(self, db: str, store: VectorStore, embedder, batch: int = 32):
        self.db, self.store, self.embedder, self.batch = db, store, embedder, batch
        self.lock = threading.Lock()          # guards the in-memory matrix
        self.encode_lock = threading.Lock()   # one encode at a time (query vs backfill)
        self.fingerprint = None
        self.chunks: list[dict] = []          # all current chunks
        self.ready: list[dict] = []           # chunks with a vector, aligned with matrix rows
        self.matrix = None
        self.pending = 0
        self.last_error = None

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)

    def refresh(self) -> bool:
        """Sync chunks and vectors with the index; True if anything changed."""
        conn = self._db()
        try:
            fp = index_fingerprint(conn)
            if fp == self.fingerprint and not self.pending:
                return False
            chunks = build_chunks(conn)
        finally:
            conn.close()
        have = self.store.keys()
        wanted = {c["key"] for c in chunks}
        gone = have - wanted
        if gone:
            self.store.drop(gone)
        todo = [c for c in chunks if c["key"] not in have]
        self.chunks, self.fingerprint, self.pending = chunks, fp, len(todo)
        self._rebuild_matrix()
        t0, done = time.time(), 0
        for i in range(0, len(todo), self.batch):
            part = todo[i:i + self.batch]
            with self.encode_lock:
                vecs = self.embedder.encode([c["text"] for c in part])
            self.store.put([(c["key"], v) for c, v in zip(part, vecs)])
            done += len(part)
            self.pending = len(todo) - done
            if done % (self.batch * 20) == 0 or self.pending == 0:
                self._rebuild_matrix()
                log.info("embedded %d/%d (%.1f chunks/s)", done, len(todo),
                         done / max(time.time() - t0, 1e-6))
        if not todo:
            self._rebuild_matrix()
        return True

    def _rebuild_matrix(self) -> None:
        vecs = self.store.load([c["key"] for c in self.chunks])
        ready = [c for c in self.chunks if c["key"] in vecs]
        if _np is not None:
            m = _np.vstack([vecs[c["key"]] for c in ready]).astype(_np.float32) if ready \
                else _np.zeros((0, self.store.dim), dtype=_np.float32)
        else:
            m = [vecs[c["key"]] for c in ready]
        with self.lock:
            self.ready, self.matrix = ready, m

    def search(self, query: str, scope: str, kinds: list[str] | None, k: int) -> dict:
        with self.encode_lock:
            q = self.embedder.encode([query])[0]
        with self.lock:
            ready, m = self.ready, self.matrix
        kinds = set(kinds or ())
        idx = [i for i, c in enumerate(ready) if c["scope"] == scope and (not kinds or c["kind"] in kinds)]
        if not idx:
            sims = []
        elif _np is not None:
            sims = (m[idx] @ _np.asarray(q, dtype=_np.float32)).tolist()
        else:
            sims = [sum(a * b for a, b in zip(m[i], q)) for i in idx]
        order = sorted(range(len(idx)), key=lambda j: -sims[j])[:max(1, min(int(k), 100))]
        total = len(self.chunks)
        return {"hits": [as_hit(ready[idx[j]], sims[j], SNIPPET) for j in order],
                "coverage": {"embedded": total - self.pending, "total": total}}

    def health(self) -> dict:
        return {"status": "OK", "model": self.embedder.name, "dim": self.store.dim,
                "chunks": len(self.chunks), "pending": self.pending, "fingerprint": self.fingerprint,
                "last_error": self.last_error}

    def loop(self, interval: float, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.refresh()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep serving, retry next round
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("refresh failed")
            stop.wait(interval)


def make_server(index: SemanticIndex, host: str, port: int) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("loopback addresses only")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._send(200, index.health())
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/search":
                return self._send(404, {"error": "not found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                q = (req.get("query") or "").strip()
                if not q or not req.get("scope"):
                    return self._send(400, {"error": "query and scope are required"})
                self._send(200, index.search(q, req["scope"], req.get("kinds"), req.get("k", 30)))
            except Exception as exc:  # noqa: BLE001
                log.exception("search failed")
                self._send(500, {"error": type(exc).__name__})

        def log_message(self, *a):  # quiet
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m nexus.semantic_service")
    p.add_argument("--db", required=True, help="Nexus database (opened read-only)")
    p.add_argument("--vectors", required=True, help="vector store (SQLite, created if missing)")
    p.add_argument("--backend", choices=("st", "hash"), default="st")
    p.add_argument("--model", default="BAAI/bge-m3")
    p.add_argument("--max-seq", type=int, default=512)
    p.add_argument("--threads", type=int, default=None, help="torch CPU threads")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8767)
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--once", action="store_true", help="embed everything pending, then exit")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    emb = HashEmbedder() if a.backend == "hash" else STEmbedder(a.model, a.max_seq, a.threads)
    index = SemanticIndex(a.db, VectorStore(a.vectors, emb.name, emb.dim), emb)
    if a.once:
        index.refresh()
        print(json.dumps(index.health(), ensure_ascii=False))
        return
    stop = threading.Event()
    threading.Thread(target=index.loop, args=(a.interval, stop), daemon=True).start()
    srv = make_server(index, a.host, a.port)
    log.info("semantic service on %s:%d (%s)", a.host, a.port, emb.name)
    try:
        srv.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
