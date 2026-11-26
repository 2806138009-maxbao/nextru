"""Agent-facing tool surface: four intent-level tools over ``service.dispatch``.

Task 001–007 exposed eleven fine-grained operations. Cold-start agents needed
15–56 calls per question, and most of that time went on round trips rather than
on Nexus itself. These four tools wrap the same read operations (every call
still goes through ``service.dispatch``, so scope checks, the Current-only rule
and path redaction are unchanged) and return compact results:

* ``nexus_search``  — find things (empty query = catalog of objects + relation types)
* ``nexus_read``    — read one object: Current facts + document/ASR paragraphs
* ``nexus_related`` — edges around one object, each with its evidence quote
* ``nexus_trace``   — provenance of one item id

This module has no transport dependency; ``mcp_server`` registers these
functions as MCP tools and the tests call them directly.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .core import NexusCore
from .native import DOC_TYPE as NATIVE_DOC_TYPE, NativeDocs, NativeError
from .freshness import LiveCheck, snapshot_info, with_live
from .search import query_groups
from .service import dispatch

SNIPPET = 160

INSTRUCTIONS = """Nexus is the user's working knowledge base: objects (mostly documents), their
Current facts (aspects), document/ASR paragraphs with stable anchors, and typed relations with
provenance. Two kinds of documents:
- Nexus-native documents (type nexus_native_document, ids ndoc/...): Nexus is their source of
  truth. They are versioned - every change is a new revision, older revisions stay readable.
- Mirrored documents (e.g. Google Docs): an imported snapshot of a source version. They are
  read-only here; `source` on nexus_read gives the snapshot version and its date.
  Mirrored documents are filed PART_OF folder entities (ent/folder/..., mirroring their Drive
  folders, nested the same way) and LINKS_TO the documents their text hyperlinks to; nexus_related
  on a document or folder shows both.
  `status_declared` is what a document says about itself (its Status line / state fields / title
  tags): currency CURRENT, DRAFT, HISTORICAL, SUPERSEDED, WITHDRAWN or UNDECLARED, with the quoted
  declaration. HISTORICAL / SUPERSEDED / WITHDRAWN documents are history, not Current - search ranks
  them behind current ones unless the question asks about the past. A mirrored snapshot is never
  Current by itself: who owns a fact is decided by its declared Owner (domain entities ent/domain/...
  from the Repository Pointer Manifest: INDEX_REF, OWNS_CURRENT), not by where it is stored.
  Relation names follow the Manifest vocabulary; PART_OF is its BELONGS_TO.
  Every document read carries `identity`: what it is, where it sits (folder path / collection),
  its domain with that domain's current owner and rule, what it supersedes or is superseded by,
  and which documents it links to or is cited by. Read it first - it tells you what you are
  looking at before the text does. nexus_read(view="card") returns only that and the opening
  paragraphs: use it to orient, and read the body (anchors / offset) only when the question
  needs what the body says - this keeps the context small.
  `ideas` (on search and document reads): principles the user wrote for their future self
  (ent/idea/..., from 00｜原则库) whose "for when" matches the situation. They are the user's own
  stance - let them shape the answer where they fit (e.g. do not reopen an accepted trade-off),
  naturally and without quoting them every time; ignore them when they do not fit.
Relations are VERIFIED (confirmed by the owner; confirmed_by="policy (plain filing)" marks plain
filing such as a record PART_OF its collection, confirmed automatically) or UNVERIFIED (proposed by
an agent or extracted by a machine, shown as candidate=true with the evidence they rest on).
check_source=true marks an agent's reading of a decision, obligation or authority: before acting on
it (sending something, changing a plan, telling the user something is settled), read its quoted
source paragraph and rely on that, not on the edge. Agent-made decision / principle / requirement
entities are summaries in the same way - quote the source when it matters.

How to work (usually 2–4 calls are enough):
1. nexus_search(query) to find where something is written. Codes match across spellings
   (dlc1 = DLC-01 = DLC01). The result lists the best-matching `documents` with their
   snapshot version and opening paragraphs, and the top paragraph hits with their text -
   often enough to answer or to pick exactly which anchors to read. Several lookups at once:
   nexus_search(queries=["…", "…"]). Empty query lists every object and the relation types.
2. nexus_read(object_id) to read an object. Documents and ASR transcripts are read
   here: their paragraphs come back with anchors (e.g. h2.3-a1b2c3d4). Use
   anchors=[...] to fetch specific paragraphs; use offset to page. `source` says which
   snapshot you are reading and, under `live`, whether the source still has that version
   (SAME / CHANGED / NOT_CHECKED / UNAVAILABLE) - no separate source lookup is needed for that.
3. nexus_related(object_id) for edges. Edges marked candidate=true are UNVERIFIED:
   each carries the evidence it rests on; they are usable leads, not owner-confirmed facts.
4. nexus_trace(item_id) when you need the source behind a fact or edge.
   nexus_resolve(target) when the answer depends on what is Current or who decides (a domain's
   current owner, whether a document is superseded, conflicting owners) - before acting on it.
5. Learning / course questions ("继续学 X", "我学到哪了", which questions practise a topic, what is
   independent vs guided): start with nexus_learning_context(topic) - one call returns the course
   register's knowledge node, Learner_State, prerequisites with their states, the questions /
   examples with the Owner's coverage boundaries, and the latest learner receipts, all anchored.
   Empty topic = every knowledge node with its state. Read documents only for what it lacks.

Status meanings: OK = answered; NOT_FOUND = nothing matched inside this scope (not
proof of absence elsewhere); PARTIAL = truncated or incomplete (see note);
UNKNOWN = the object has no readable material of that kind; INVALID_SCOPE = bad
argument or scope.
Anchors identify paragraphs (for citing or re-reading). Status labels written inside a
document (draft, proposal, decided...) are part of its content; Current facts carry their own
verification label."""


def _snip(body: str, groups: list[list[str]], width: int = 140) -> str:
    low = body.lower()
    pos = min((low.find(t.lower()) for g in groups for t in g if t.lower() in low), default=0)
    start = max(0, pos - width // 3)
    return ("…" if start else "") + body[start:start + width] + ("…" if start + width < len(body) else "")


def _status(env: dict) -> str:
    return (env.get("execution") or {}).get("status") or env.get("status") or "UNKNOWN"


def _err(env: dict) -> dict:
    error = env.get("error") or {}
    return {"status": _status(env), "message": error.get("message"),
            "detail": error.get("detail")}


_HISTORICAL = re.compile(r"HISTORICAL|HISTORY|SUPERSEDED|DEPRECATED|RETIRED|LEGACY|ARCHIVED", re.I)
_ASKS_HISTORY = re.compile(r"旧|历史|以前|之前|当时|早期|原来|history|historical|superseded|legacy|old|previous", re.I)
TOP_DOCUMENTS = 3          # documents summarised in a search result
DOCUMENT_POOL = 40         # hits considered when ranking documents (only `limit` are shown)
MORE_PER_DOCUMENT = 5      # other matching paragraphs listed per document
_BARE_ANCHOR = re.compile(r"h\d+\.\d+")


def _spectrum_item(i: dict) -> dict:
    """One spectrum entry, short: the edges themselves (with quotes) are already in `edges`."""
    out = {"object_id": i["object_id"], "name": i["name"], "tier": i["tier"]}
    if i.get("gate"):
        out["gate"] = True
    b = i["basis"]
    if "relations" in b:
        out["via"] = sorted({r["type"] for r in b["relations"]})
    elif "shared_neighbours" in b:
        out["via"] = [n["name"] or n["object_id"] for n in b["shared_neighbours"]]
    elif "words" in b:
        out["words"] = b["words"]
    return out


def _mark_auto(conn, edges: list[dict]) -> None:
    """Say how far an edge may be leaned on: confirmed_by for policy-confirmed filing, check_source
    for an agent's UNVERIFIED interpretation of a decision, obligation or authority."""
    ids = [e["relation_id"] for e in edges]
    if not ids:
        return
    try:
        rows = conn.execute(
            "SELECT relation_id, decided_by, json_extract(payload, '$.review') FROM native_proposals"
            f" WHERE kind = 'RELATION' AND relation_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    except Exception:          # stores without native tables
        return
    by = {r[0]: (r[1], r[2]) for r in rows}
    for e in edges:
        decided, tier = by.get(e["relation_id"], (None, None))
        if e.get("verification") == "VERIFIED" and decided == "policy:auto":
            e["confirmed_by"] = "policy (plain filing)"
        elif e.get("verification") == "UNVERIFIED" and tier == "GATE":
            e["check_source"] = True


def _doc_status(conn, object_id: str) -> dict | None:
    """The document's self-declared status (doc_status aspect), trimmed for agents."""
    try:
        r = conn.execute("SELECT payload FROM aspects WHERE object_id = ? AND name = 'doc_status'"
                         " AND is_current = 1", (object_id,)).fetchone()
    except Exception:
        return None
    if r is None:
        return None
    p = json.loads(r[0])
    out = {"currency": p.get("currency"), "basis": p.get("currency_basis")}
    for k in ("object_type", "mission_state", "plan_state", "authority_class", "superseded_by"):
        if p.get(k):
            out[k] = p[k]
    if p.get("declared"):
        out["declared"] = [{"text": d["text"][:160], "anchor": d["anchor"]} for d in p["declared"][:2]]
    return out


_RETIRED = ("HISTORICAL", "SUPERSEDED", "WITHDRAWN")
CARD_CHARS = 1500


def _full_segments(store, object_id: str, ref: str) -> list[dict]:
    """All paragraph segments of one reading, in the core's order and through its redaction."""
    from .redaction import scrub_local_paths
    from .research import _segments
    segments, _ = _segments(store, object_id, ref, {})
    return scrub_local_paths({"segments": segments})[0]["segments"]


EXPAND_MAX_NEIGHBOURS = 10 # nexus_related(expand_facts=True): neighbours whose facts are inlined
EXPAND_MAX_FACTS = 8       # upper bound on facts per neighbour
MULTI_MAX = 8              # queries accepted in one multi-query call
MULTI_HITS = 4             # hits per query in a multi-query result
MULTI_TEXT = 2             # of which this many keep their paragraph text
MULTI_DOCS = 2             # documents per query (only the first keeps its opening paragraphs)
HEAD_PARAGRAPHS = 2        # opening paragraphs after the title (status lines usually sit here)
HEAD_CHARS = 500
TEXT_HITS = 4              # top paragraph hits returned with their text
TEXT_CHARS = 700


class AgentTools:
    def __init__(self, core: NexusCore, default_scope: str, live_check: LiveCheck | None = None):
        self.core = core
        self.scope = default_scope
        self.live_check = live_check

    def _documents(self, pool: list[dict], shown: set[str], scope: str,
                   groups: list[list[str]]) -> list[dict]:
        """Best-matching documents over a wider hit pool than the hits shown.

        Ranked by the summed score of each document's best three paragraph matches plus a small
        title bonus; a self-labelled historical document yields to a current one on a tie. Each
        entry lists the document's other matching paragraphs (not already in the shown hits), so an
        agent can see that a document matches in several places without another search.
        """
        store = self.core._store
        stats: dict[str, dict] = {}
        for rank, h in enumerate(pool):
            s = stats.setdefault(h["object_id"], {"hits": 0, "best": rank, "scores": [], "more": []})
            s["hits"] += 1
            s["scores"].append(h.get("score") or 0)
            if h.get("anchor") and h["anchor"] not in shown and len(s["more"]) < MORE_PER_DOCUMENT:
                s["more"].append({"anchor": h["anchor"], "snippet": h.get("snippet")})
        docs = []
        wants_history = any(_ASKS_HISTORY.search(t) for g in groups for t in g)
        for oid, s in stats.items():
            obj = store.get_object(oid)
            if obj is None or obj["scope"] != scope:
                continue
            name = (obj["display_name"] or "").lower()
            in_title = sum(1 for g in groups if any(t.lower() in name for t in g if len(t) >= 2))
            st = _doc_status(store.conn, oid)
            historical = int((st or {}).get("currency") in _RETIRED
                             or bool(_HISTORICAL.search(obj["display_name"] or "")))
            score = sum(sorted(s["scores"])[-3:]) + 2 * in_title
            if historical and not wants_history:  # history still shows, behind current documents
                score *= 0.5
            docs.append((-score, historical, s["best"], oid, obj))
        out = []
        for *_rank, oid, obj in sorted(docs, key=lambda d: d[:3])[:TOP_DOCUMENTS]:
            info = snapshot_info(store, oid)
            entry = {"object_id": oid, "name": obj["display_name"], "type": obj["type"],
                     "matches": stats[oid]["hits"]}
            st = _doc_status(store.conn, oid)
            if st and st["currency"] != "UNDECLARED":
                entry["status_declared"] = st["currency"] + (
                    f" (superseded by {st['superseded_by']})" if st.get("superseded_by") else "")
            more = self._doc_matches(info["source_ref_id"], groups, shown) if info else stats[oid]["more"]
            if more:
                entry["also_matching"] = more
            if info:
                entry["source"] = {k: info[k] for k in ("snapshot_version", "source_modified",
                                                        "snapshot_taken")}
                secs = store.conn.execute(
                    "SELECT anchor, body FROM source_sections WHERE source_ref_id = ?"
                    " ORDER BY ordinal LIMIT ?", (info["source_ref_id"], 1 + HEAD_PARAGRAPHS)).fetchall()
                entry["head"] = [{"anchor": s["anchor"], "text": s["body"][:HEAD_CHARS]}
                                 for s in secs[1:]]
            out.append(entry)
        return out

    def _doc_matches(self, ref_id: str, groups: list[list[str]], shown: set[str]) -> list[dict]:
        """Paragraphs of one document matching the most query groups, in document order on ties.

        Searched inside the document itself, so a paragraph that matches only one short term (and
        would never reach the global top hits of a large store) is still listed for a top document.
        """
        rows = self.core._store.conn.execute(
            "SELECT anchor, body, ordinal FROM source_sections WHERE source_ref_id = ?", (ref_id,)).fetchall()
        ranked = []
        for r in rows:
            low = r["body"].lower()
            n = sum(1 for g in groups if any(t.lower() in low for t in g))
            if n:
                ranked.append((-n, r["ordinal"] or 0, r["anchor"], r["body"]))
        out = []
        for _n, _o, anchor, body in sorted(ranked):
            if anchor in shown:
                continue
            out.append({"anchor": anchor, "snippet": _snip(body, groups)})
            if len(out) >= MORE_PER_DOCUMENT:
                break
        return out

    def _current_facts(self, object_id: str, scope: str, limit: int | None = None) -> list[dict]:
        """Current facts of one object through get_current_state (scope, conflicts, Authority honoured)."""
        store = self.core._store
        out = []
        for name in store.list_aspect_names(object_id):
            if name == "content" or store.get_aspect(object_id, name, current_only=True) is None:
                continue
            env = dispatch(self.core, "get_current_state",
                           {"object_id": object_id, "aspect": name, "scope": scope})
            state = env.get("state") or {}
            ev = env.get("evidence") or []
            fact = {"aspect": name, "status": _status(env), "value": state.get("value"),
                    "verification": state.get("verification"), "freshness": state.get("freshness"),
                    "sources": [f"{e.get('source_ref_id')}#{e.get('anchor')}" for e in ev]}
            conflict = (env.get("result") or {}).get("conflict_candidates") or state.get("conflict_candidates")
            if conflict:
                fact["conflict_candidates"] = conflict
            out.append(fact)
            if limit and len(out) >= limit:
                break
        return out

    def _scope(self, scope: str | None) -> str:
        return scope or self.scope

    # ---------------------------------------------------------------- search
    def nexus_search(self, query: str = "", scope: str | None = None, limit: int = 8,
                     queries: list[str] | None = None) -> dict:
        """One query, or several at once (`queries`): one round trip instead of one step per item."""
        scope = self._scope(scope)
        qs = [q for q in ([query] if (query or "").strip() else []) + list(queries or []) if (q or "").strip()]
        if queries and qs:
            return self._search_many(qs[:MULTI_MAX], scope, limit)
        return self._search_one(query, scope, limit)

    def _search_many(self, qs: list[str], scope: str, limit: int) -> dict:
        per = max(1, min(int(limit), MULTI_HITS))
        results = []
        for q in qs:
            r = self._search_one(q, scope, per)
            if r.get("status") not in ("OK", "NOT_FOUND"):
                results.append({"query": q, **r})
                continue
            hits = r.get("hits", [])
            for h in hits[MULTI_TEXT:]:
                h.pop("text", None)                      # keep the combined result compact
            docs = []
            for i, d in enumerate(r.get("documents", [])[:MULTI_DOCS]):
                d = dict(d)
                if i:
                    d.pop("head", None)
                if d.get("also_matching"):
                    d["also_matching"] = d["also_matching"][:3]
                docs.append(d)
            results.append({"query": q, "status": r["status"], "hits": hits,
                            "total_matches": r.get("total_matches", 0), "documents": docs})
        ok = any(x.get("status") == "OK" for x in results)
        return {"status": "OK" if ok else "NOT_FOUND", "scope": scope, "mode": "multi", "results": results}

    def _search_one(self, query: str, scope: str, limit: int) -> dict:
        if not (query or "").strip():
            objs = dispatch(self.core, "list_objects", {"scope": scope, "limit": 200})
            if _status(objs) != "OK":
                return _err(objs)
            rels = dispatch(self.core, "list_relation_types", {"scope": scope})
            return {"status": "OK", "scope": scope, "mode": "catalog",
                    "objects": objs["result"]["objects"],
                    "objects_truncated": objs["result"]["truncated"],
                    "relation_types": [
                        {"type": t["relation_type"], "count": t["count"],
                         "by_verification": t["by_verification"]}
                        for t in (rels.get("result") or {}).get("relation_types", [])]}
        limit = max(1, min(int(limit), 50))
        env = dispatch(self.core, "search", {"query": query, "scope": scope,
                                             "limit": max(limit, DOCUMENT_POOL)})
        if _status(env) not in ("OK", "NOT_FOUND"):
            return _err(env)
        result = env.get("result") or {}
        pool = result.get("hits", [])
        raw_shown = pool[:limit]
        hits = [{k: h.get(k) for k in ("kind", "object_id", "item_id", "anchor", "snippet")}
                for h in raw_shown]
        for h, raw in zip(hits, raw_shown):
            if raw.get("match") == "semantic":    # found by meaning, no query word in it
                h["match"] = "semantic"
        store = self.core._store
        shown = 0
        for h, raw in zip(hits, raw_shown):
            if shown < TEXT_HITS and h["kind"] == "native":
                row = store.conn.execute(
                    "SELECT b.body, t.title FROM native_documents d JOIN native_tree t"
                    " ON t.doc_id = d.doc_id AND t.revision = d.current_revision AND t.node_id = ?"
                    " LEFT JOIN native_blobs b ON b.sha256 = t.body_sha256", (h["item_id"],)).fetchone()
                if row:
                    body = row["body"] or row["title"] or ""
                    h["text"] = body[:TEXT_CHARS] + ("…" if len(body) > TEXT_CHARS else "")
                    shown += 1
                continue
            if shown >= TEXT_HITS or h["kind"] != "paragraph" or not raw.get("source_ref_id"):
                continue
            row = store.conn.execute("SELECT body FROM source_sections WHERE source_ref_id = ? AND anchor = ?",
                                     (raw["source_ref_id"], h["anchor"])).fetchone()
            if row:
                h["text"] = row["body"][:TEXT_CHARS] + ("…" if len(row["body"]) > TEXT_CHARS else "")
                shown += 1
        out = {"status": _status(env), "scope": scope, "hits": hits,
               "total_matches": result.get("total_candidates", 0),
               "truncated": bool(result.get("truncated")) or len(pool) > limit}
        if hits:
            out["documents"] = self._documents(pool, {h["anchor"] for h in hits if h.get("anchor")},
                                               scope, query_groups(query))
        if not hits:
            out["note"] = ("no text matched in this scope; try fewer or different words, or an "
                           "empty query to list all objects")
        if env.get("warnings"):
            out["warnings"] = env["warnings"]
        if result.get("semantic") == "unavailable":
            out["semantic"] = "unavailable (text matches only)"
        from .ideas import associate, IDEAS_NOTE
        ideas = associate(self.core._store.conn, scope, query)
        if ideas:
            out["ideas"] = ideas
            out["ideas_note"] = IDEAS_NOTE
        return out

    # ------------------------------------------------------------------ read
    def nexus_read(self, object_id: str, scope: str | None = None,
                   anchors: list[str] | None = None, offset: int = 0,
                   max_chars: int = 8000, revision: int | None = None, view: str = "full") -> dict:
        """view="card": identity card + status + the opening paragraphs only (about CARD_CHARS),
        for "what is this / is it still valid / what goes with it" - read more only if needed."""
        out = self._nexus_read(object_id, scope, anchors, offset,
                               min(max_chars, CARD_CHARS) if view == "card" and not anchors else max_chars,
                               revision)
        if view == "card" and not anchors and isinstance(out, dict) and out.get("status") in ("OK", "PARTIAL"):
            out["view"] = "card"
            out["view_note"] = ("identity card and opening paragraphs only; call nexus_read again with "
                                "anchors or offset (view='full') if the question needs the body")
        return out

    def _nexus_read(self, object_id: str, scope: str | None, anchors: list[str] | None, offset: int,
                    max_chars: int, revision: int | None) -> dict:
        scope = self._scope(scope)
        store = self.core._store
        obj = store.get_object(object_id or "")
        if obj is not None and obj["type"] == NATIVE_DOC_TYPE and obj["scope"] == scope:
            return self._read_native(object_id, scope, anchors, offset, max_chars, revision)
        if obj is None or obj["scope"] != scope:
            probe = dispatch(self.core, "list_objects", {"scope": scope, "limit": 1})
            if _status(probe) != "OK":
                return _err(probe)
            return {"status": "NOT_FOUND", "message": f"no object {object_id!r} in scope {scope!r}",
                    "hint": "use nexus_search to find object ids"}
        current = self._current_facts(object_id, scope)
        observations = []
        for row in store.get_observations(object_id):
            if row["dimension"] in ("document_paragraph", "relation_evidence", "entity_mention"):
                continue
            try:
                detail = json.loads(row["detail"]) if row["detail"] else {}
            except ValueError:
                detail = {}
            refs = json.loads(row["source_ref_ids"] or "[]")
            observations.append({"id": row["observation_id"], "dimension": row["dimension"],
                                 "statement": row["statement"], "stance": row["stance"],
                                 "anchor": detail.get("anchor") if isinstance(detail, dict) else None,
                                 "sources": refs})
        mentions = store.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE object_id = ? AND dimension = 'entity_mention'",
            (object_id,)).fetchone()[0]
        paragraphs: list[dict] = []
        total = 0
        material_status = None
        wanted: set[str] | None = None
        env = dispatch(self.core, "get_research_evidence",
                       {"object_id": object_id, "scope": scope, "max_chars": 400000})
        if _status(env) == "NOT_FOUND":
            material_status = "NONE"
        elif (env.get("result") or {}).get("readings") is not None:
            material_status = _status(env)
            for reading in env["result"]["readings"]:
                lifecycle = reading["aspect"]["lifecycle"]
                if lifecycle == "SUPERSEDED":
                    continue
                ref = reading["identity"].get("source_ref_id")
                if anchors:
                    wanted = {self._resolve_anchor(ref, a) or a for a in anchors}
                segments = reading.get("segments", [])
                cov = reading.get("coverage") or {}
                if ref and len(segments) < (cov.get("segment_count") or 0):
                    # The core trims segments to its 40k-char JSON budget. nexus_read pages on its
                    # own, so take the full list (same order, same redaction) instead of the cut one.
                    segments = _full_segments(store, object_id, ref)
                for seg in segments:
                    if wanted is not None and seg.get("anchor") not in wanted:
                        continue
                    paragraphs.append({"anchor": seg.get("anchor"), "text": seg.get("text"),
                                       "source": ref, "lifecycle": lifecycle})
        else:
            material_status = _status(env)
        total = len(paragraphs)
        page, used = [], 0
        for p in paragraphs[max(0, int(offset)):]:
            size = len(p["text"] or "") + 40
            if page and used + size > max_chars:
                break
            page.append(p)
            used += size
        next_offset = max(0, int(offset)) + len(page)
        out: dict[str, Any] = {
            "status": "OK" if (current or page or observations) else (
                "UNKNOWN" if material_status != "NONE" else "OK"),
            "object": {"object_id": object_id, "type": obj["type"], "name": obj["display_name"]},
        }
        info = snapshot_info(store, object_id)
        if info:
            info = with_live(info, self.live_check)
            out["source"] = {k: info[k] for k in ("snapshot_version", "source_modified",
                                                  "snapshot_taken", "live")}
        from .card import identity_card
        from .ideas import associate, IDEAS_NOTE, LIBRARY
        card = identity_card(store.conn, scope, object_id) if obj["type"] == "document" else None
        if obj["type"] == "document" and object_id != LIBRARY:
            head = " ".join((p.get("text") or "")[:300] for p in paragraphs[:3])
            ideas = associate(store.conn, scope, (obj["display_name"] or "") + " " + head)
            if ideas:
                out["ideas"] = ideas
                out["ideas_note"] = IDEAS_NOTE
        if card:
            out["identity"] = card
        st = _doc_status(store.conn, object_id)
        if st:
            out["status_declared"] = st
            if st["currency"] in _RETIRED:
                out["status_hint"] = ("this document declares itself " + st["currency"] + "; it is history, "
                                      "not Current" + (f" - Current is {st['superseded_by']}"
                                                       if st.get("superseded_by") else ""))
        out.update({
            "current_facts": current,
            "observations": observations,
            "paragraphs": page,
            "paragraphs_total": total,
        })
        if mentions:
            out["extracted_mentions"] = mentions
            out["mentions_hint"] = "use nexus_related to see where this entity is mentioned"
        if next_offset < total:
            out["status"] = "PARTIAL"
            out["next_offset"] = next_offset
            out["complete"] = False
            out["note"] = (f"{total - next_offset} more paragraph(s); call again with "
                           f"offset={next_offset}, or pass anchors=[...] from nexus_search")
        elif total and int(offset) >= total and not anchors:
            out["status"] = "OK"
            out["complete"] = False
            out["note"] = (f"offset counts paragraphs, not characters: this document has {total} "
                           f"paragraph(s). Use offset=0..{total - 1}, or anchors=[...]")
        elif total:
            # said explicitly: agents re-paged complete reads to make sure nothing was cut off
            out["complete"] = True
            start = max(0, int(offset))
            out["returned"] = (f"all {total} requested paragraph(s)" if anchors else
                               f"paragraphs {start + 1}-{total} of {total}: end of document, "
                               "nothing more to read")
        if anchors:
            got = {p["anchor"] for p in paragraphs}
            refs = {p["source"] for p in paragraphs} or {None}
            missing = [a for a in anchors
                       if a not in got and not any(self._resolve_anchor(r, a) in got
                                                   for r in refs if r)]
            if missing:
                out["anchors_not_found"] = missing
                out["anchors_hint"] = ("anchors must be copied from nexus_search / nexus_read; "
                                       "the bare position (e.g. h0.209) also works")
        if object_id.startswith("ent/"):
            # an entity is mostly its links: give them here, so one read shows where it sits
            edges = []
            for r in store.conn.execute(
                    "SELECT r.relation_id, r.from_id, r.relation_type, r.to_id, r.verification,"
                    " f.display_name AS from_name, t.display_name AS to_name FROM relations r"
                    " JOIN objects f ON f.object_id = r.from_id JOIN objects t ON t.object_id = r.to_id"
                    " WHERE (r.from_id = ? OR r.to_id = ?) AND r.scope = ? AND r.expired_at IS NULL"
                    " ORDER BY r.relation_type", (object_id, object_id, scope)):
                out_dir = r["from_id"] == object_id
                edges.append({"relation_id": r["relation_id"], "type": r["relation_type"],
                              "direction": "out" if out_dir else "in",
                              "other": r["to_id"] if out_dir else r["from_id"],
                              "other_name": r["to_name"] if out_dir else r["from_name"],
                              "verification": r["verification"]})
            _mark_auto(store.conn, edges)
            out["edges"] = edges
            out["edges_hint"] = "nexus_related(object_id) gives each edge's quoted evidence"
        if not current and not paragraphs and not observations and not out.get("edges"):
            out["note"] = "object exists but has no Current facts and no readable paragraphs"
        return out

    def _read_native(self, doc_id: str, scope: str, anchors: list[str] | None, offset: int,
                     max_chars: int, revision: int | None) -> dict:
        try:
            doc = NativeDocs(self.core._store.conn).read(doc_id, revision, scope=scope)
        except NativeError as e:
            return e.as_dict()
        nodes = doc.pop("nodes")
        if anchors:
            want = set(anchors)
            nodes = [n for n in nodes if n["node_id"] in want]
            missing = sorted(want - {n["node_id"] for n in nodes})
            if missing:
                doc["anchors_not_found"] = missing
        page, used = [], 0
        for n in nodes[max(0, int(offset)):]:
            size = len(n.get("text") or n.get("title") or "") + 60
            if page and used + size > max_chars:
                break
            page.append(n)
            used += size
        nxt = max(0, int(offset)) + len(page)
        doc["nodes"] = page
        doc["nodes_total"] = len(nodes)
        revs = NativeDocs(self.core._store.conn).history(doc_id)["revisions"]
        doc["history"] = [{k: r[k] for k in ("revision", "author", "message", "created_at", "restored_from")}
                          for r in revs[-10:]]
        rels = [{"relation_id": r["relation_id"], "type": r["relation_type"], "from": r["from_id"],
                 "to": r["to_id"], "verification": r["verification"]}
                for r in self.core._store.conn.execute(
                    "SELECT * FROM relations WHERE (from_id = ? OR to_id = ?) AND expired_at IS NULL",
                    (doc_id, doc_id))]
        if rels:
            doc["relations"] = rels
        from .card import identity_card
        card = identity_card(self.core._store.conn, scope, doc_id)
        if card:
            doc["identity"] = card
        if nxt < len(nodes):
            doc.update(status="PARTIAL", next_offset=nxt, complete=False)
        else:
            doc["complete"] = True
        return doc

    def _resolve_anchor(self, ref: str | None, anchor: str) -> str | None:
        """Exact anchor or alias; a bare position such as 'h0.209' (no hash) maps to that section."""
        if not ref or not anchor:
            return None
        store = self.core._store
        hit = store.resolve_anchor(ref, anchor)
        if hit is None and _BARE_ANCHOR.fullmatch(anchor):
            row = store.conn.execute(
                "SELECT anchor FROM source_sections WHERE source_ref_id = ? AND anchor LIKE ?",
                (ref, anchor + "-%")).fetchone()
            hit = row[0] if row else None
        return hit

    # --------------------------------------------------------------- related
    def _edge_evidence(self, rel: dict, scope: str) -> list[dict]:
        store = self.core._store
        quotes = []
        for row in store.conn.execute(
                "SELECT statement, detail, source_ref_ids FROM observations"
                " WHERE dimension = 'relation_evidence' AND detail LIKE ?",
                (f'%"relation_id": "{rel["relation_id"]}"%',)):
            detail = json.loads(row["detail"])
            ref = json.loads(row["source_ref_ids"])[0]
            quotes.append({"quote": row["statement"], "source": ref, "anchor": detail.get("anchor"),
                           "note": detail.get("note")})
        if quotes:
            return quotes
        # Hand-authored edges: show what the store recorded at each provenance ref
        # (an observation statement citing it, else the section body).
        for ref_id in rel.get("provenance") or rel.get("provenance_refs") or []:
            ref = store.get_source_ref(ref_id)
            if ref is None or ref["status"] != "OK" or ref["access_scope"] != scope:
                quotes.append({"source": ref_id, "withheld": True})
                continue
            obs = store.conn.execute(
                "SELECT statement FROM observations WHERE source_ref_ids LIKE ? LIMIT 1",
                (f'%"{ref_id}"%',)).fetchone()
            secs = store.get_sections(ref_id)
            text = obs["statement"] if obs else (secs[0]["body"] if secs else None)
            if text:
                quotes.append({"source": ref_id, "anchor": ref["anchor"],
                               "quote": text[:SNIPPET],
                               "kind": "observation" if obs else "section"})
        return quotes

    def nexus_related(self, object_id: str, scope: str | None = None,
                      include_candidates: bool = True, max_depth: int = 1,
                      expand_facts: bool = False, max_facts: int = 6,
                      possibly_related: bool = True, spectrum: bool = True) -> dict:
        scope = self._scope(scope)
        env = dispatch(self.core, "traverse_relations", {
            "object_id": object_id, "scope": scope, "direction": "both",
            "max_depth": max(1, min(3, int(max_depth))),
            "include_candidates": bool(include_candidates)})
        result = env.get("result") or {}
        if "edges" not in result:
            return _err(env)
        edges = []
        for e in result.get("edges", []):
            edges.append({"relation_id": e["relation_id"], "from": e["from_id"],
                          "type": e["relation_type"], "to": e["to_id"],
                          "verification": e["verification"], "candidate": e.get("candidate"),
                          "other": (e.get("other_object") or {}).get("display_name"),
                          "evidence": self._edge_evidence(e, scope)})
        _mark_auto(self.core._store.conn, edges)
        if expand_facts:
            # Optional, bounded: the neighbours' current facts with sources, so a question about
            # "all X of this project" needs one call instead of one read per neighbour.
            per = max(1, min(int(max_facts), EXPAND_MAX_FACTS))
            seen: dict[str, list] = {}
            for e in edges:
                other = e["to"] if e["from"] == object_id else e["from"]
                if other not in seen and len(seen) < EXPAND_MAX_NEIGHBOURS:
                    seen[other] = self._current_facts(other, scope, limit=per)
                if other in seen:
                    e["other_id"] = other
                    e["other_facts"] = seen[other]
            if len({e["to"] if e["from"] == object_id else e["from"] for e in edges}) > EXPAND_MAX_NEIGHBOURS:
                out_note = f"facts expanded for the first {EXPAND_MAX_NEIGHBOURS} neighbours only"
            else:
                out_note = None
        else:
            out_note = None
        out = {"status": _status(env), "object_id": object_id, "edges": edges,
               "candidate_edges": sum(1 for e in edges if e["candidate"]),
               "excluded_candidates": result.get("candidate_count", 0) - result.get(
                   "included_candidates", 0)}
        if not edges:
            out["note"] = ("no edges around this object" if include_candidates else
                           "no VERIFIED edges; pass include_candidates=true to see candidates")
        if out_note:
            out["expand_note"] = out_note
        if possibly_related:
            from .linking import POSSIBLY_NOTE, suggest
            try:
                maybe = suggest(self.core._store.conn, object_id, scope)
            except Exception as e:                 # a lead must never break the edge listing
                out["possibly_related_error"] = f"{type(e).__name__}: {e}"
            else:
                if maybe:
                    out["possibly_related"] = [{k: m[k] for k in ("object_id", "name", "type", "score")
                                                if k in m} | ({"similar_siblings": m["similar_siblings"]}
                                                              if m.get("similar_siblings") else {})
                                               for m in maybe]
                    out["possibly_related_note"] = POSSIBLY_NOTE
        if spectrum:
            from .relatedness import SPECTRUM_NOTE, spectrum as _spectrum
            try:
                sp = _spectrum(self.core._store.conn, object_id, scope)
            except Exception as e:                 # a lead must never break the edge listing
                out["spectrum_error"] = f"{type(e).__name__}: {e}"
            else:
                if sp["items"]:
                    out["spectrum"] = [_spectrum_item(i) for i in sp["items"]]
                    out["spectrum_counts"] = {k: v for k, v in sp["counts"].items() if v}
                    out["spectrum_note"] = SPECTRUM_NOTE
        return out

    # ----------------------------------------------------------------- trace
    def nexus_learning_context(self, topic: str = "", course: str | None = None,
                               scope: str | None = None, max_receipts: int = 5) -> dict:
        from .ideas import TASK_NOTE, for_task
        from .learning import learning_context
        scope = self._scope(scope)
        out = learning_context(self.core._store, scope, topic=topic, course=course,
                               max_receipts=max_receipts, live_check=self.live_check)
        method = for_task(self.core._store.conn, scope, "learning")
        if method:      # first in the result, and also when the course has no register (NOT_FOUND)
            out = {"method": method, "method_note": TASK_NOTE, **out}
        return out

    def nexus_resolve(self, target: str, scope: str | None = None) -> dict:
        from .resolve import resolve
        scope = self._scope(scope)

        def search(q: str) -> list[str]:
            res = self._search_one(q, scope, 8)
            return [d["object_id"] for d in res.get("documents", [])]
        return {"status": "OK", **resolve(self.core._store.conn, scope, target, search=search,
                                          live_check=self.live_check)}

    def nexus_trace(self, item_id: str, scope: str | None = None) -> dict:
        scope = self._scope(scope)
        env = dispatch(self.core, "trace_evidence", {"item_id": item_id, "scope": scope})
        if _status(env) not in ("OK", "PARTIAL"):
            return _err(env)
        result = env.get("result") or {}
        sources = [{k: e.get(k) for k in ("source_ref_id", "source_system", "native_id",
                                          "revision", "anchor", "status", "retrieved_at")}
                   for e in env.get("evidence") or []]
        return {"status": _status(env), "item": result.get("item"), "sources": sources,
                "inaccessible": result.get("inaccessible_source_refs", []),
                "warnings": env.get("warnings") or []}


WRITE_INSTRUCTIONS = """Write tools are enabled in this session. Writes take effect immediately (no
confirmation step) and every one can be undone; history is never lost.
- nexus_create_document / nexus_edit_document write native documents and return doc_id and
  revision. Mirrored documents cannot be written (MIRROR_READ_ONLY) - put new content in a native
  document instead.
- To edit: nexus_read first, then pass its revision as expected_revision and the node_id values
  it returned. VERSION_CONFLICT means the document changed in between - read again and redo.
- nexus_create_object makes a typed entity (person, decision, principle, ...; id ent/<type>/...)
  that relations can point at; nexus_add_relation links two existing objects. An agent's entities
  and relations need evidence ({object_id, anchor} of a mirrored paragraph, {doc_id, node_id} of a
  native paragraph, or a source_ref_id); they are searchable / appear in nexus_related at once.
  Nothing waits for the owner and you do not ask them to confirm. The result's `review`: AUTO =
  plain filing (PART_OF / about / RECORDED_IN / LISTED_IN, evidence inside one endpoint, no
  decision/principle/requirement/mission endpoint), confirmed by policy; QUIET = UNVERIFIED lead;
  GATE = UNVERIFIED interpretation of a decision, obligation or authority (check_source: rely on
  the source paragraph, not the edge, before acting). Never present an UNVERIFIED edge as settled.
- Links: create/edit results carry suggested_links - existing objects the text most likely
  belongs with, found by the server (no search needed). Link the ones that really belong
  (nexus_add_relation; evidence = the paragraph of your document that refers to it), usually 0-3;
  ignore the rest. They are not relations until you add them.
- Collections: file a recurring kind of record (e.g. a training log) under one existing entity with
  PART_OF; search for it first (aliases cover other names) instead of creating a parallel one.
- Undo: nexus_restore_revision (an older revision becomes current again), nexus_archive_document
  (undo a create; still readable) and nexus_remove_relation."""

TOOL_DESCRIPTIONS = {
    "nexus_search": (
        "Find objects, document paragraphs, observations and Current facts by text. "
        "Returns hits with ids, anchors and snippets (the top paragraph hits include their "
        "text) plus the best-matching documents with snapshot version and opening paragraphs. "
        "Chinese works; 2-character terms are fine; codes match across spellings (dlc1 = DLC-01). "
        "To look up several things at once, pass queries=[...] (up to 8): one call returns a compact "
        "result per query. Call with an empty query to list every object and the relation types."),
    "nexus_read": (
        "Read one object: its Current facts (with verification/freshness) and, for "
        "documents and ASR transcripts, the paragraphs with stable anchors, plus `source`: "
        "snapshot version, source modified time and a live check of whether the source still "
        "has that version. Pass anchors=[...] (from nexus_search) to get just those "
        "paragraphs (a bare position such as h0.209 also works); offset is a paragraph index; "
        "use the next_offset it gives when status is PARTIAL. Every document read starts with "
        "`identity` (what it is, where it sits, its domain and owner, lineage, links). "
        "view='card' returns just that plus the opening paragraphs - enough for 'what is this / "
        "is it still valid / what goes with it'; read the body only when the question needs it."),
    "nexus_related": (
        "Edges around one object, each with the verbatim quote it rests on. "
        "candidate=true means UNVERIFIED (agent-proposed or machine-extracted): a lead, not an "
        "owner-confirmed fact. "
        "Set include_candidates=false to see VERIFIED edges only. expand_facts=true also returns "
        "each neighbour's current facts (status, verification, sources; up to max_facts each, "
        "10 neighbours) - e.g. every item of a project in one call. possibly_related (on by default) "
        "lists up to 5 objects it is not linked to but most likely belongs with, computed from titles: "
        "leads, not relations. spectrum lists every related object once, strongest first: A verified "
        "relation / B verified filing or link (usable as fact), C unverified (a lead; gate=true: read "
        "the source before acting), D structurally close / E its title's words appear here (hints: "
        "read before citing, never link just because it is listed)."),
    "nexus_create_document": (
        "Create a Nexus-native document now (revision 1). children is a list; each item is a "
        "paragraph string, or {kind:'section', title, children:[...]} (sections nest), or "
        "{kind:'paragraph', body}. Paragraph text is plain text (no leading '- '). The result's "
        "suggested_links are existing objects this document most likely belongs with (computed by the "
        "server, nothing stored): link the ones that really belong, ignore the rest. Undo with "
        "nexus_archive_document."),
    "nexus_edit_document": (
        "Change a Nexus-native document now; makes a new revision, older ones stay readable. "
        "expected_revision = the current revision from nexus_read (else VERSION_CONFLICT). "
        "ops: [{op:set_text,node_id,body} | {op:set_title,node_id,title} | "
        "{op:insert,parent_id,after,kind:paragraph|section,body|title} (parent_id null = top level, "
        "after null = first) | {op:delete,node_id}]. Drive-mirrored documents cannot be edited "
        "(MIRROR_READ_ONLY). Undo with nexus_restore_revision."),
    "nexus_add_relation": (
        "Add a typed relation between two existing objects (e.g. a native document -> the "
        "profile or document it is about). evidence (required for agents): a list of "
        "{object_id, anchor} mirrored paragraphs, {doc_id, node_id} native paragraphs or "
        "source_ref_ids; the quoted text is kept with the edge. The result's review says how it was "
        "stored: AUTO (plain filing, confirmed by policy), QUIET (UNVERIFIED lead) or GATE "
        "(UNVERIFIED interpretation, check_source before acting on it). No owner confirmation is "
        "needed; it shows up in nexus_related at once. "
        "Undo with nexus_remove_relation."),
    "nexus_create_object": (
        "Create a typed entity with a stable id independent of any file (ent/<type>/<id>): a person, "
        "decision, principle, path, requirement, event, ... type is one short lowercase word. aliases "
        "make it findable by other names. evidence (required for agents): [{object_id, anchor}] "
        "paragraphs of mirrored documents, {doc_id, node_id} native paragraphs, or source_ref_ids. "
        "Agent entities are stored UNVERIFIED (no confirmation needed). Search first to avoid a "
        "duplicate; then link it with nexus_add_relation."),
    "nexus_restore_revision": (
        "Undo edits: make an older revision of a native document the current one again (as a new "
        "revision; nothing is deleted). expected_revision = the current revision."),
    "nexus_archive_document": (
        "Undo a create: archive a native document (gone from search, still readable with its "
        "history). archived=false restores it."),
    "nexus_remove_relation": "Undo nexus_add_relation for one relation_id.",
    "nexus_register_add_row": (
        "Add a row to a course register that lives in Nexus (e.g. course='reg/demo0020'). type: the tab - "
        "'learner_receipt' for a class/learning receipt, 'knowledge', 'question', 'example', 'resource' or "
        "'evidence'. row_id: the row's own id (e.g. 'R:013', 'nyquist'). fields: the cells as written, e.g. "
        "for a receipt {date, knowledge_ids: 'shape;fourier', evidence_class, verbatim_learner_receipt}; "
        "knowledge_ids become EVIDENCE_FOR links to those knowledge rows. Your rows are UNVERIFIED. "
        "Registers still mirrored from a Sheet are read-only (MIRROR_READ_ONLY)."),
    "nexus_register_edit": (
        "Change cells of one register row in Nexus. aspect: 'learner_state' (capability_state, "
        "last_observed_date, latest_receipt_id, next_independent_evidence_gate, ...; created if the row has "
        "none yet), 'register_row' (canonical_label, epistemic_state, notes, ...) or 'receipt'. Read the row "
        "first (nexus_read / nexus_learning_context) and pass its revision as expected_revision (0 when "
        "creating learner_state); VERSION_CONFLICT means re-read and redo. set_fields: {cell: value}; "
        "unset: cells to drop. Each change is a new revision; your edits are UNVERIFIED."),
    "nexus_resolve": (
        "Resolve who owns the current truth before relying on it or acting (Repository Pointer Manifest "
        "resolution). target: an object_id, a declared id (MIS-/PRG-/CAP-...), a domain name (EXU, LIFE, "
        "伦敦生活...) or free text. Returns the domain, its index and current owner with the owner "
        "strategy, the most specific current document (a retired target points to its successor), "
        "freshness, Manifest-typed relationships and reverse impact, a bounded working set, and a state: "
        "KNOWN, STALE, CONFLICTED (never resolved silently) or UNKNOWN, plus domain_state (NOT_CHECKED = the "
        "declared owner is not in Nexus). Free text that names no domain is not routed by guess: it returns "
        "search candidates - pick one and resolve its object_id. Use it when a question "
        "turns on what is current, who decides, or before acting on a document."),
    "nexus_learning_context": (
        "Course learning context in ONE call, from an imported course CORE REGISTER: give a topic "
        "(e.g. '零 ISI', 'Nyquist', 'noise', a node id) and get the matching knowledge node with its "
        "Learner_State Current (state, last observed, evidence boundary, next independent gate), its "
        "prerequisites and their states, the questions/examples/lectures pointing at it with the Owner's "
        "DIRECT/PARTIAL/NOT_COVERED coverage text and relation rationale, and the latest learner receipts "
        "(newest first). Every item has an anchor. Empty topic lists all knowledge nodes with their state. "
        "Use it first for lesson prep, 'where am I', which questions fit, independent vs guided. "
        "When the user has written down how they want to learn, it comes first under `method` (also when "
        "the course has no register): plan from it before choosing materials; `bound` lists the sessions, "
        "corrections and sources that belong to it, each with a link to the original where there is one."),
    "nexus_trace": (
        "Provenance of one item id (aspect:<object>/<name>, a relation_id, or an "
        "observation id): source system, native id, revision, anchor."),
}
