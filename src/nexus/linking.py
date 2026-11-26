"""Link suggestions: which existing objects a document most likely belongs with.

Why this exists (2026-11-10): agents were told to keep writes fast — one PART_OF to a fixed home,
no searching just to add relations. A cross-domain entry ("学习入口｜产品运营 × 商业分析 × 创业金融")
then landed with no link to the 自媒体 research or Opportunity Radar documents it builds on,
although the agent knew they existed. Finding those candidates is cheap for the server (titles of
~1k objects, a few milliseconds) and expensive for an agent (a search round trip each), so the
server does it: every native write returns `suggested_links`, and nexus_related lists
`possibly_related` for any object. Suggestions are computed, never stored — they are not
relations, carry no Authority and leave the graph untouched until someone adds an edge.

Scoring (title-based, deliberately simple): tokens are CJK character bigrams and Latin words
(URLs dropped, filler words such as LIVING / 归档 / 副本 ignored), weighted by rarity across object
titles. A candidate scores by how much of its own title the document mentions (rare tokens count
more) plus a bonus for tokens the two titles share; profiles, LIVING masters and indexes get a small
boost (they are what a new record usually belongs with). Then:

* a folder is replaced by its index document (00｜… / 索引 / INDEX / 入口) when it has one, and
  left out otherwise (filing homes come from the fixed-home table, not from suggestions);
* members of one series (same parent, near-identical title) collapse into the best one, which
  reports how many look-alikes it stands for;
* objects already related to the document are left out (they are in nexus_related already).
"""

from __future__ import annotations

import collections
import math
import re
import sqlite3

URL = re.compile(r"https?://\S+|www\.\S+")
CJK_RUN = re.compile(r"[㐀-鿿豈-﫿]+")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.+\-]{2,}")
LATIN_STOP = frozenset(
    "living draft index project the and for with from current canonical candidate review source "
    "notes note case study standard contract batch copy archive pilot ver version".split())
CJK_STOP = frozenset("归档 副本 记录 讨论 笔记 草稿 索引 入口 总纲 档副".split())
INDEX_TITLE = re.compile(r"^\s*00\s*[｜|]|索引|INDEX|入口|START HERE|PROJECT", re.I)
SKIP_TYPES = frozenset({"aspect", "scope"})
MAX_TEXT = 20000          # characters of the document read for matching
DEFAULT_LIMIT = 5
MIN_SCORE = 12.0          # below this a match is one or two common words; not worth showing
HUB_TITLE = re.compile(r"PROFILE|LIVING|主档|总纲|CANONICAL|^\s*00\s*[｜|]|索引|INDEX|入口", re.I)
HUB_BONUS = 1.3           # a profile / living master / index outranks one item of its series
SERIES_SIMILARITY = 0.35  # Jaccard of title tokens for two siblings to count as one series
SERIES_UNFILED = 0.5      # the same for two objects with no parent at all


def tokens(text: str) -> set[str]:
    text = URL.sub(" ", text or "")
    out: set[str] = set()
    for run in CJK_RUN.findall(text):
        for i in range(len(run) - 1):
            b = run[i:i + 2]
            if b not in CJK_STOP:
                out.add(b)
    for w in LATIN.findall(text):
        w = w.lower().strip(".-+")
        # "finite-difference" also counts as "finite" + "difference": titles hyphenate what body
        # text writes as two words
        for part in (w, *(w.split("-") if "-" in w else ())):
            if len(part) >= 3 and part not in LATIN_STOP and not re.fullmatch(r"v?[\d.\-]+", part):
                out.add(part)
    return out


class _Catalog:
    """Title tokens of every live object in a scope, rebuilt when objects change."""

    def __init__(self, conn: sqlite3.Connection, scope: str):
        self.scope = scope
        self.key = self._key(conn, scope)
        rows = conn.execute(
            "SELECT o.object_id, o.type, o.display_name FROM objects o"
            " LEFT JOIN native_documents n ON n.doc_id = o.object_id"
            " WHERE o.scope = ? AND n.archived_at IS NULL", (scope,)).fetchall()
        self.objects: dict[str, dict] = {}
        for r in rows:
            if r["type"] in SKIP_TYPES or not r["display_name"]:
                continue
            t = tokens(r["display_name"])
            if t:
                self.objects[r["object_id"]] = {"type": r["type"], "name": r["display_name"], "t": t}
        df = collections.Counter(tok for o in self.objects.values() for tok in o["t"])
        n = max(len(self.objects), 1)
        self.idf = {tok: math.log(n / (1 + c)) + 0.1 for tok, c in df.items()}
        self.parent: dict[str, str] = {}
        for r in conn.execute(
                "SELECT from_id, to_id FROM relations WHERE expired_at IS NULL AND scope = ?"
                " AND lower(relation_type) = 'part_of'", (scope,)):
            self.parent.setdefault(r["from_id"], r["to_id"])
        self.index_doc: dict[str, str] = {}
        for child, folder in self.parent.items():
            o = self.objects.get(child)
            if o and folder not in self.index_doc and INDEX_TITLE.search(o["name"]) \
                    and self.objects.get(folder, {}).get("type") == "folder":
                self.index_doc[folder] = child

    @staticmethod
    def _key(conn: sqlite3.Connection, scope: str) -> tuple:
        a = conn.execute("SELECT COUNT(*), IFNULL(MAX(rowid), 0) FROM objects WHERE scope = ?",
                         (scope,)).fetchone()
        b = conn.execute("SELECT COUNT(*) FROM native_documents WHERE archived_at IS NOT NULL").fetchone()
        c = conn.execute("SELECT COUNT(*), IFNULL(MAX(rowid), 0) FROM relations"
                         " WHERE lower(relation_type) = 'part_of' AND expired_at IS NULL").fetchone()
        return (tuple(a), b[0], tuple(c))


_CACHE: dict[tuple[int, str], _Catalog] = {}


def _catalog(conn: sqlite3.Connection, scope: str) -> _Catalog:
    k = (id(conn), scope)
    cat = _CACHE.get(k)
    if cat is None or cat.key != _Catalog._key(conn, scope):
        cat = _Catalog(conn, scope)
        _CACHE[k] = cat
    return cat


def object_text(conn: sqlite3.Connection, object_id: str) -> str:
    """Title plus body text of a native document, a mirrored document or an entity."""
    o = conn.execute("SELECT display_name FROM objects WHERE object_id = ?", (object_id,)).fetchone()
    parts = [o["display_name"] or ""] if o else []
    d = conn.execute("SELECT current_revision FROM native_documents WHERE doc_id = ?",
                     (object_id,)).fetchone()
    if d is not None:
        for r in conn.execute(
                "SELECT t.kind, t.title, b.body FROM native_tree t"
                " LEFT JOIN native_blobs b ON b.sha256 = t.body_sha256"
                " WHERE t.doc_id = ? AND t.revision = ? ORDER BY t.rowid", (object_id, d[0])):
            parts.append((r["title"] if r["kind"] == "section" else r["body"]) or "")
    else:
        ref = conn.execute(
            "SELECT source_ref_id FROM aspects WHERE object_id = ? AND name = 'content'"
            " ORDER BY revision DESC LIMIT 1", (object_id,)).fetchone()
        if ref is not None:
            for r in conn.execute("SELECT body FROM source_sections WHERE source_ref_id = ?"
                                  " ORDER BY ordinal LIMIT 400", (ref[0],)):
                parts.append(r["body"] or "")
        for r in conn.execute("SELECT alias FROM entity_aliases WHERE object_id = ?", (object_id,)):
            parts.append(r["alias"] or "")
    return "\n".join(parts)[:MAX_TEXT]


def _linked(conn: sqlite3.Connection, object_id: str) -> set[str]:
    out = set()
    for r in conn.execute("SELECT from_id, to_id FROM relations WHERE expired_at IS NULL"
                          " AND (from_id = ? OR to_id = ?)", (object_id, object_id)):
        out.add(r["to_id"] if r["from_id"] == object_id else r["from_id"])
    return out


def suggest(conn: sqlite3.Connection, object_id: str, scope: str, limit: int = DEFAULT_LIMIT,
            text: str | None = None, exclude_linked: bool = True) -> list[dict]:
    """Up to `limit` existing objects the document most likely belongs with, best first."""
    cat = _catalog(conn, scope)
    me = cat.objects.get(object_id)
    full = tokens(text if text is not None else object_text(conn, object_id))
    mine = me["t"] if me else set()
    skip = {object_id} | (_linked(conn, object_id) if exclude_linked else set())
    idf = cat.idf
    scored = []
    for oid, o in cat.objects.items():
        if oid in skip:
            continue
        shared = o["t"] & full
        if not shared:
            continue
        mass = sum(idf[t] for t in shared)
        cover = mass / sum(idf[t] for t in o["t"])
        score = mass * cover + 2 * sum(idf[t] for t in o["t"] & mine)
        if HUB_TITLE.search(o["name"]):
            score *= HUB_BONUS
        if score >= MIN_SCORE:
            scored.append((score, oid, shared))
    scored.sort(key=lambda x: (-x[0], x[1]))

    out: list[dict] = []
    taken: set[str] = set(skip)
    for score, oid, shared in scored:
        target = cat.index_doc.get(oid, oid)
        if target in taken or cat.objects[target]["type"] == "folder":
            continue                       # a folder without an index document is only a filing place
        o = cat.objects[target]
        twin = next((x for x in out if _series(cat, target, x["object_id"])), None)
        taken.add(target)
        if twin is not None:
            twin["similar_siblings"] = twin.get("similar_siblings", 0) + 1
            continue
        if len(out) >= limit:
            continue                       # keep scanning only to count siblings of those shown
        entry = {"object_id": target, "name": o["name"], "type": o["type"],
                 "score": round(score, 1),
                 "matched": sorted(shared, key=lambda t: -idf[t])[:6]}
        if target != oid:
            entry["via_folder"] = oid
        out.append(entry)
    return out


def _series(cat: _Catalog, a: str, b: str) -> bool:
    """Two members of one series: same parent and similar titles, or no parent and near-identical
    titles (e.g. a run of pilot-result documents)."""
    pa, pb = cat.parent.get(a), cat.parent.get(b)
    j = _jaccard(cat.objects[a]["t"], cat.objects[b]["t"])
    if pa and pa == pb:
        return j >= SERIES_SIMILARITY
    return pa is None and pb is None and j >= SERIES_UNFILED


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(len(a | b), 1)


SUGGEST_NOTE = ("suggested_links: existing objects this document most likely belongs with (computed "
                "from titles; not relations, nothing was stored). Link the ones that really belong with "
                "nexus_add_relation (evidence: the paragraph of this document that refers to it); ignore "
                "the rest. similar_siblings = more look-alike members of the same series.")
POSSIBLY_NOTE = ("possibly_related: computed look-alikes by title, not relations - a lead for what this "
                 "object may belong with; read before relying on one.")
