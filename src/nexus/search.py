"""Full-text discovery index (SQLite FTS5, trigram tokenizer).

What is indexed (one row per item, always tagged with the owning object's scope):

* ``object``     display name, id and type
* ``paragraph``  ingested document paragraphs (``document_paragraph`` observations)
* ``observation`` every other observation statement
* ``aspect``     the text values of CURRENT aspect payloads
* ``native``     sections and paragraphs of the current revision of Nexus-native documents
  (kept up to date per document by ``reindex_native`` on every native write)

The index is derived data. It is rebuilt automatically whenever the
underlying tables change (cheap fingerprint check), so callers never see a
stale index and no write path has to remember to update it.

Ranking is a deliberately simple P1 heuristic: the query is split into terms
(whitespace/punctuation; long CJK runs also yield overlapping bigrams); terms of
>= 3 characters go through FTS5 ``MATCH`` (trigram = substring), shorter terms
through ``LIKE``. A hit scores the summed length of the distinct terms it
contains; FTS5 ``bm25`` breaks ties. Relevance is a discovery signal only — it
confers no Authority and promotes nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

KINDS = ("object", "paragraph", "observation", "aspect", "native")
SNIPPET_CHARS = 140

DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    body, kind UNINDEXED, item_id UNINDEXED, object_id UNINDEXED, scope UNINDEXED,
    source_ref_id UNINDEXED, anchor UNINDEXED, tokenize = 'trigram'
);
CREATE TABLE IF NOT EXISTS search_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_SPLIT = re.compile(r"[\s,.;:!?，。；：！？、（）()\[\]【】「」『』“”\"'《》<>|｜/\\·•—\-]+")


def _flatten(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _flatten(v)
    elif isinstance(value, list):
        for v in value:
            yield from _flatten(v)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


def _fingerprint(conn: sqlite3.Connection) -> str:
    parts = []
    for table, extra in (("objects", "0"), ("observations", "TOTAL(LENGTH(statement))"),
                         ("aspects", "TOTAL(is_current) + TOTAL(LENGTH(payload))"),
                         ("source_refs", "TOTAL(LENGTH(status))")):
        row = conn.execute(f"SELECT COUNT(*), IFNULL(MAX(rowid),0), {extra} FROM {table}").fetchone()
        parts.append(f"{table}:{row[0]}:{row[1]}:{row[2]}")
    return "|".join(parts)


def ensure_index(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    fp = _fingerprint(conn)
    row = conn.execute("SELECT value FROM search_meta WHERE key = 'fingerprint'").fetchone()
    if row and row[0] == fp:
        return
    rebuild(conn)
    conn.execute("INSERT OR REPLACE INTO search_meta(key, value) VALUES ('fingerprint', ?)", (fp,))


def rebuild(conn: sqlite3.Connection) -> int:
    conn.executescript(DDL)
    conn.execute("DELETE FROM search_index")
    rows: list[tuple] = []
    for o in conn.execute("SELECT object_id, type, display_name, scope FROM objects"):
        rows.append(_object_row(conn, o))
    rows += _observation_rows(conn, None)
    rows += _aspect_rows(conn, None)
    conn.executemany("INSERT INTO search_index(body, kind, item_id, object_id, scope,"
                     " source_ref_id, anchor) VALUES (?,?,?,?,?,?,?)", rows)
    # entities the owner rejected keep their id (history) but stay out of search
    conn.execute("DELETE FROM search_index WHERE object_id IN (SELECT object_id FROM aspects"
                 " WHERE name = 'entity' AND is_current = 1 AND verification = 'DISPUTED')")
    has_native = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'native_documents'").fetchone()
    extra = 0
    if has_native:
        for (doc_id,) in conn.execute("SELECT doc_id FROM native_documents").fetchall():
            extra += reindex_native(conn, doc_id)
    return len(rows) + extra


def _object_row(conn: sqlite3.Connection, o) -> tuple:
    """An object's own row: its name, id, type and (for entities) its aliases."""
    names = [o["display_name"], o["object_id"], o["type"]]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'entity_aliases'").fetchone():
        names += [r[0] for r in conn.execute("SELECT alias FROM entity_aliases WHERE object_id = ?"
                                             " AND alias != ?", (o["object_id"], o["display_name"] or ""))]
    body = " ".join(x for x in names if x)
    return (body, "object", o["object_id"], o["object_id"], o["scope"], None, None)


def _observation_rows(conn: sqlite3.Connection, object_id: str | None) -> list[tuple]:
    rows = []
    # relation_evidence rows quote text that is indexed where it lives (a paragraph); indexing the
    # copy would only duplicate hits and keep e.g. an archived document findable through a quote.
    q = ("SELECT b.observation_id, b.object_id, b.dimension, b.statement, b.detail,"
         " b.source_ref_ids, o.scope FROM observations b JOIN objects o ON o.object_id = b.object_id"
         " WHERE b.dimension != 'relation_evidence'")
    for b in conn.execute(q + (" AND b.object_id = ?" if object_id else ""),
                          (object_id,) if object_id else ()):
        try:
            detail = json.loads(b["detail"]) if b["detail"] else {}
        except (TypeError, ValueError):
            detail = {}
        try:
            refs = json.loads(b["source_ref_ids"]) or []
        except (TypeError, ValueError):
            refs = []
        kind = "paragraph" if b["dimension"] == "document_paragraph" else "observation"
        anchor = detail.get("anchor") if isinstance(detail, dict) else None
        rows.append((b["statement"], kind, b["observation_id"], b["object_id"], b["scope"],
                     refs[0] if refs else None, anchor))
    return rows


def _aspect_rows(conn: sqlite3.Connection, object_id: str | None) -> list[tuple]:
    rows = []
    q = ("SELECT a.object_id, a.name, a.payload, a.source_ref_id, o.scope FROM aspects a"
         " JOIN objects o ON o.object_id = a.object_id WHERE a.is_current = 1")
    for a in conn.execute(q + (" AND a.object_id = ?" if object_id else ""),
                          (object_id,) if object_id else ()):
        try:
            payload = json.loads(a["payload"])
        except (TypeError, ValueError):
            payload = a["payload"]
        body = " ".join(_flatten(payload))
        if body.strip():
            rows.append((f"{a['name']} {body}", "aspect", f"aspect:{a['object_id']}/{a['name']}",
                         a["object_id"], a["scope"], a["source_ref_id"], None))
    return rows


def reindex_object(conn: sqlite3.Connection, object_id: str) -> int:
    """Replace one (non-document) object's index rows - its name/aliases, observations and current
    aspects - exactly as rebuild() would build them. Runs inside the caller's transaction."""
    conn.execute("DELETE FROM search_index WHERE object_id = ? AND kind IN ('object', 'observation',"
                 " 'paragraph', 'aspect')", (object_id,))
    o = conn.execute("SELECT object_id, type, display_name, scope FROM objects WHERE object_id = ?",
                     (object_id,)).fetchone()
    if o is None:
        return 0
    rows = [_object_row(conn, o)] + _observation_rows(conn, object_id) + _aspect_rows(conn, object_id)
    conn.executemany("INSERT INTO search_index(body, kind, item_id, object_id, scope,"
                     " source_ref_id, anchor) VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _native_rows(conn: sqlite3.Connection, doc_id: str) -> list[tuple]:
    doc = conn.execute("SELECT * FROM native_documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if doc is None or ("archived_at" in doc.keys() and doc["archived_at"]):
        return []
    out = []
    # the document itself (its title), as in rebuild(), so a title-only word finds the document
    o = conn.execute("SELECT object_id, type, display_name, scope FROM objects WHERE object_id = ?",
                     (doc_id,)).fetchone()
    if o is not None:
        body = " ".join(x for x in (o["display_name"], o["object_id"], o["type"]) if x)
        out.append((body, "object", o["object_id"], o["object_id"], o["scope"], None, None))
    for r in conn.execute(
            "SELECT t.node_id, t.kind, t.title, b.body FROM native_tree t"
            " LEFT JOIN native_blobs b ON b.sha256 = t.body_sha256"
            " WHERE t.doc_id = ? AND t.revision = ?", (doc_id, doc["current_revision"])):
        text = r["title"] if r["kind"] == "section" else r["body"]
        if text and text.strip():
            out.append((text, "native", r["node_id"], doc_id, doc["scope"], None, r["node_id"]))
    return out


def reindex_native(conn: sqlite3.Connection, doc_id: str) -> int:
    """Replace one native document's index rows with its current revision. The index table must
    exist already (``ensure_index`` / ``rebuild``); this runs inside the caller's transaction."""
    conn.execute("DELETE FROM search_index WHERE kind IN ('native', 'object') AND object_id = ?", (doc_id,))
    rows = _native_rows(conn, doc_id)
    conn.executemany("INSERT INTO search_index(body, kind, item_id, object_id, scope,"
                     " source_ref_id, anchor) VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


# Mixed runs such as "dlc1现在是什么进度" are split at the Latin/CJK boundary.
_SCRIPT_RUN = re.compile(r"[A-Za-z0-9]+|[^A-Za-z0-9]+")
# Identifier-like tokens: letters followed by a number ("dlc1", "DLC01", "demo0008", "Q4").
_IDENT = re.compile(r"^([A-Za-z]{1,12})0*(\d{1,6})$")
_JOINED_IDENT = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,12})[-_](\d{1,6})(?![A-Za-z0-9])")


def identifier_variants(tok: str) -> list[str]:
    """Spellings a person writes for the same code: dlc1 -> dlc1, DLC-1, DLC01, DLC-01.

    Query-side only (the index is untouched); hyphenated variants are kept as one FTS phrase.
    """
    m = _IDENT.match(tok)
    if not m:
        return [tok]
    prefix, num = m.group(1), m.group(2)
    nums = {num, num.zfill(2)} | ({num.zfill(4)} if len(num) <= 4 and len(num) >= 3 else set())
    out = [tok]
    for n in sorted(nums, key=len):
        for v in (f"{prefix}{n}", f"{prefix}-{n}"):
            if v.lower() not in {o.lower() for o in out}:
                out.append(v)
    return out


def query_groups(text: str) -> list[list[str]]:
    """Query terms grouped by alternative spellings; a hit scores each group once."""
    groups: list[list[str]] = []
    # keep "DLC-01" / "DEMO_0008" together before punctuation splitting removes the hyphen
    text = _JOINED_IDENT.sub(lambda m: m.group(1) + m.group(2), text or "")
    for raw in _SPLIT.split(text):
        for tok in _SCRIPT_RUN.findall(raw.strip()):
            tok = tok.strip()
            if not tok:
                continue
            if len(tok) == 1 and not _CJK.match(tok):
                continue
            groups.append(identifier_variants(tok))
            if _CJK.search(tok) and len(tok) > 4:
                groups.extend([tok[i:i + 2]] for i in range(len(tok) - 1))
    seen: set[str] = set()
    out = []
    for g in groups:
        key = g[0].lower()
        if key not in seen:
            seen.add(key)
            out.append(g)
    return out


def query_terms(text: str) -> list[str]:
    return [t for g in query_groups(text) for t in g]


def _snippet(body: str, terms: list[str]) -> str:
    low = body.lower()
    pos = min((low.find(t.lower()) for t in terms if t.lower() in low), default=0)
    start = max(0, pos - SNIPPET_CHARS // 3)
    text = body[start:start + SNIPPET_CHARS]
    return ("…" if start else "") + text + ("…" if start + SNIPPET_CHARS < len(body) else "")


def search(conn: sqlite3.Connection, text: str, scope: str,
           kinds: Iterable[str] | None = None, limit: int = 10) -> dict:
    ensure_index(conn)
    groups = query_groups(text)
    terms = [t for g in groups for t in g]
    kinds = [k for k in (kinds or KINDS) if k in KINDS]
    if not terms or not kinds:
        return {"terms": terms, "hits": [], "total_candidates": 0}
    kind_ph = ",".join("?" * len(kinds))
    hits: dict[int, dict] = {}

    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t for t in terms if len(t) < 3]
    if long_terms:
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
        for r in conn.execute(
                f"SELECT rowid, *, bm25(search_index) AS bm FROM search_index"
                f" WHERE search_index MATCH ? AND scope = ? AND kind IN ({kind_ph})",
                [match, scope, *kinds]):
            hits[r["rowid"]] = dict(r)
    for t in short_terms:
        # (body || '') keeps the LIKE out of FTS5: older SQLite (e.g. 3.37) hands a bare
        # `body LIKE` to the trigram index, which cannot match patterns under 3 characters and
        # returns nothing; newer versions fall back to a scan. The expression scans everywhere.
        for r in conn.execute(
                f"SELECT rowid, *, 0.0 AS bm FROM search_index"
                f" WHERE (body || '') LIKE ? AND scope = ? AND kind IN ({kind_ph})",
                [f"%{t}%", scope, *kinds]):
            hits.setdefault(r["rowid"], dict(r))

    scored = []
    for h in hits.values():
        low = h["body"].lower()
        matched = [t for t in terms if t.lower() in low]
        if not matched:
            continue
        # one score per group: alternative spellings of the same code count once
        score = sum(max((len(t) for t in g if t.lower() in low), default=0) for g in groups)
        scored.append((score, -(h["bm"] or 0.0), h, matched))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["item_id"]))
    text_hits = [{"kind": h["kind"], "item_id": h["item_id"], "object_id": h["object_id"],
                  "source_ref_id": h["source_ref_id"], "anchor": h["anchor"],
                  "score": score, "matched_terms": matched}
                 for score, _bm, h, matched in scored]
    bodies = {h["item_id"]: (h["body"], matched) for _s, _b, h, matched in scored}

    from . import semantic
    sem = semantic.semantic_hits(text, scope, kinds, max(30, 2 * limit)) \
        if semantic.url() else {"status": "off", "hits": []}
    if sem["status"] == "ok" and sem["hits"]:
        merged = semantic.merge(text_hits, sem["hits"])
    else:
        merged = text_hits
    out = []
    for h in merged[:limit]:
        if h["item_id"] in bodies:
            body, matched = bodies[h["item_id"]]
            h["snippet"] = _snippet(body, matched)
        out.append(h)
    result = {"terms": terms, "hits": out, "total_candidates": len(merged)}
    if sem["status"] != "off":
        result["semantic"] = sem["status"]
    return result
