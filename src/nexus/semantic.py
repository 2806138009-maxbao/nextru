"""Semantic recall for ``search``: chunking, the client of the embedding service, and the merge.

Full-text search (``search.py``) only finds what is written: "伦敦学习模型" never reaches a
document titled "伦敦生活模型". A second, optional channel finds text by meaning. It runs in a
separate process (``nexus.semantic_service``) that holds the embedding model and the vectors, so
the Nexus core stays dependency-free and a missing or slow service only means "text matches only".

* Chunks are built from the full-text index itself (``search_index``): consecutive paragraphs of
  one document are joined into windows of about ``CHUNK_TARGET`` characters, prefixed with the
  document title; an object's name and current aspects form one chunk; other observations are one
  chunk each. A chunk key is a hash of its scope, object and text, so only new or changed text is
  ever embedded again.
* ``semantic_hits`` asks the service (``NEXUS_SEMANTIC_URL``) for the closest chunks.
* ``merge`` combines the two ranked lists by reciprocal rank fusion. A text hit inside a
  semantically matching chunk is boosted (match "both"); a chunk with no text hit becomes a hit of
  its own (match "semantic"), pointing at its first paragraph.

Like text relevance, similarity is a discovery signal only - it confers no Authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.request
from typing import Any, Iterable

CHUNK_TARGET = 400      # characters per window of consecutive paragraphs
CHUNK_MAX = 700         # a single paragraph longer than this is split
OBJECT_MAX = 1200       # characters of an object's name + aspects chunk
RRF_K = 60
TIMEOUT = float(os.environ.get("NEXUS_SEMANTIC_TIMEOUT", "3"))


def url() -> str | None:
    return os.environ.get("NEXUS_SEMANTIC_URL") or None


def _key(scope: str, object_id: str, text: str) -> str:
    return hashlib.sha1(f"{scope}\x00{object_id}\x00{text}".encode("utf-8")).hexdigest()[:24]


def _pieces(body: str) -> list[str]:
    body = body.strip()
    if len(body) <= CHUNK_MAX:
        return [body] if body else []
    return [body[i:i + CHUNK_TARGET] for i in range(0, len(body), CHUNK_TARGET)]


def index_fingerprint(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COUNT(*), IFNULL(MAX(rowid), 0) FROM search_index").fetchone()
    return f"{row[0]}:{row[1]}"


def build_chunks(conn: sqlite3.Connection) -> list[dict]:
    """All chunks of the current full-text index, in index order."""
    names = {r[0]: r[1] for r in conn.execute("SELECT object_id, display_name FROM objects")}
    chunks: list[dict] = []
    per_object: dict[str, dict] = {}
    run: dict | None = None      # the open window of consecutive paragraphs

    def close() -> None:
        nonlocal run
        if run and run["parts"]:
            title = names.get(run["object_id"]) or run["object_id"]
            text = title + "\n" + "\n".join(run["parts"])
            chunks.append({"key": _key(run["scope"], run["object_id"], text), "kind": run["kind"],
                           "object_id": run["object_id"], "scope": run["scope"],
                           "item_ids": run["item_ids"], "item_id": run["item_ids"][0],
                           "anchor": run["anchor"], "source_ref_id": run["source_ref_id"],
                           "text": text})
        run = None

    for r in conn.execute("SELECT body, kind, item_id, object_id, scope, source_ref_id, anchor"
                          " FROM search_index ORDER BY rowid"):
        body, kind, item_id, object_id, scope, ref, anchor = r
        if kind in ("paragraph", "native"):
            for piece in _pieces(body or ""):
                if run and (run["object_id"] != object_id or run["kind"] != kind
                            or run["size"] >= CHUNK_TARGET):
                    close()
                if run is None:
                    run = {"object_id": object_id, "kind": kind, "scope": scope, "parts": [],
                           "item_ids": [], "anchor": anchor, "source_ref_id": ref, "size": 0}
                run["parts"].append(piece)
                if item_id not in run["item_ids"]:
                    run["item_ids"].append(item_id)
                run["size"] += len(piece)
            continue
        close()
        if kind in ("object", "aspect"):
            o = per_object.setdefault(object_id, {"scope": scope, "parts": [], "item_ids": [],
                                                  "source_ref_id": None})
            o["parts"].append(body or "")
            o["item_ids"].append(item_id)
            if kind == "aspect" and ref and not o["source_ref_id"]:
                o["source_ref_id"] = ref
        else:  # observation
            for piece in _pieces(body or ""):
                title = names.get(object_id) or object_id
                text = title + "\n" + piece
                chunks.append({"key": _key(scope, object_id, text), "kind": kind,
                               "object_id": object_id, "scope": scope, "item_ids": [item_id],
                               "item_id": item_id, "anchor": anchor, "source_ref_id": ref,
                               "text": text})
    close()
    for object_id, o in per_object.items():
        text = (names.get(object_id) or object_id) + "\n" + "\n".join(o["parts"])
        text = text[:OBJECT_MAX]
        first = next((i for i in o["item_ids"] if not str(i).startswith("aspect:")), o["item_ids"][0])
        chunks.append({"key": _key(o["scope"], object_id, text),
                       "kind": "object" if first == object_id else "aspect",
                       "object_id": object_id, "scope": o["scope"], "item_ids": o["item_ids"],
                       "item_id": first, "anchor": None, "source_ref_id": o["source_ref_id"],
                       "text": text})
    return chunks


def semantic_hits(query: str, scope: str, kinds: Iterable[str], k: int) -> dict:
    """Closest chunks from the embedding service: {"status": ..., "hits": [...]}.

    Never raises; any failure is reported as status "unavailable" and search falls back to text.
    """
    base = url()
    if not base:
        return {"status": "off", "hits": []}
    payload = json.dumps({"query": query, "scope": scope, "kinds": list(kinds), "k": k}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/search", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - the channel is optional by design
        return {"status": "unavailable", "hits": [], "error": type(exc).__name__}
    return {"status": "ok", "hits": data.get("hits", []), "coverage": data.get("coverage")}


def merge(text_hits: list[dict], sem: list[dict]) -> list[dict]:
    """Reciprocal rank fusion of ranked text hits and ranked semantic chunks.

    Every returned hit gets ``score`` (the fused score, x1000) and ``match``; a text hit keeps its
    term score as ``text_score``, a semantic one carries ``similarity``.
    """
    fused: dict[str, dict] = {}
    for rank, h in enumerate(text_hits):
        fused[h["item_id"]] = {**h, "text_score": h.get("score"), "match": "text",
                               "_rrf": 1.0 / (RRF_K + rank + 1)}
    for rank, c in enumerate(sem):
        bonus = 1.0 / (RRF_K + rank + 1)
        inside = [i for i in c.get("item_ids", []) if i in fused]
        if inside:
            for i in inside:
                h = fused[i]
                if h["match"] == "text":           # one chunk boost per text hit
                    h["match"] = "both"
                    h["similarity"] = c.get("similarity")
                    h["_rrf"] += bonus
            continue
        if c["item_id"] in fused:
            continue
        fused[c["item_id"]] = {
            "kind": c["kind"], "item_id": c["item_id"], "object_id": c["object_id"],
            "source_ref_id": c.get("source_ref_id"), "anchor": c.get("anchor"),
            "matched_terms": [], "snippet": c.get("snippet"), "match": "semantic",
            "similarity": c.get("similarity"), "text_score": None, "_rrf": bonus}
    out = sorted(fused.values(), key=lambda h: (-h["_rrf"], h["item_id"]))
    for h in out:
        h["score"] = round(h.pop("_rrf") * 1000, 3)
    return out


def snippet(text: str, width: int) -> str:
    body = text.split("\n", 1)[1] if "\n" in text else text
    return body[:width] + ("…" if len(body) > width else "")


def as_hit(chunk: dict, similarity: float, width: int) -> dict[str, Any]:
    return {k: chunk[k] for k in ("key", "kind", "object_id", "item_id", "item_ids", "anchor",
                                  "source_ref_id")} | {"similarity": round(float(similarity), 4),
                                                       "snippet": snippet(chunk["text"], width)}
