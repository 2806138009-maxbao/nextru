"""Authority resolution: the Repository Pointer Manifest's resolution algorithm over Nexus data.

`resolve(conn, scope, target)` follows the Manifest's steps with what Nexus holds - the imported
Manifest (domain entities, INDEX_REF / OWNS_CURRENT / GOVERNED_BY / BELONGS_TO), the Drive folder
structure (PART_OF), each document's self-declared status (doc_status aspect) and snapshot
freshness - and reports what it found and how sure it is. It never creates Authority:

- the domain comes from a declared pointer, an exact domain name, or the object's place in the
  folder tree under a domain's index (Source-Owned Authority: not from where text is stored);
  free text that names no domain is UNKNOWN - search candidates are listed for the agent to choose,
  never routed by guess (Manifest acceptance test E);
- the current owner is the Manifest's OWNS_CURRENT pointer read with its current_owner_strategy;
  a ref whose document is not in Nexus is NOT_CHECKED (not MISSING: Drive itself was not checked);
- conflicts are reported (CONFLICTED), never silently resolved: two current owners for a domain, a
  document that declares itself CURRENT while another SUPERSEDES it, an ID claimed by two documents;
- a retired target (HISTORICAL / SUPERSEDED / WITHDRAWN) is STALE as Current, with its successor.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .freshness import snapshot_info, with_live

PRECEDENCE = ("live external reality (when the question is live external state) > most-specific valid "
              "Current SOT > Registry / Runtime / Domain canonical > Router / Index summary > historical "
              "evidence > chat memory > model inference (Global Semantic Spine §11)")
STRATEGY_MEANING = {
    "EXPLICIT_REF": "the owner document is the current-state authority for this domain",
    "INDEX_ROUTE_TO_SPECIFIC": ("the index is the entry and summary; follow its route to the specific child "
                                "document before making a child-specific claim"),
    "EXTERNAL_REALITY_FIRST": ("the repository holds plan, context and history; live external facts (official "
                               "systems) must be refreshed from their authority before consequential action"),
    "REGISTRY_BY_OBJECT": "a registry owns reusable capability state; runtime readiness is owned separately",
    "INDEX_CURRENT_SUMMARY": "the index carries the one bounded current summary",
    "FOLDER_ONLY": "only a folder is declared; no current owner document",
}
RETIRED = ("HISTORICAL", "SUPERSEDED", "WITHDRAWN")
_ID = re.compile(r"\b(?:MIS|PRG|CAP|REP-CONTRACT|XDI|CBR)-[A-Z0-9-]*\d{8}-\d{3}\b")
MANIFEST_TYPES = ("OWNS_CURRENT", "BELONGS_TO", "PART_OF", "DEPENDS_ON", "BLOCKS", "PRODUCES", "EVIDENCE_FOR",
                  "SUPERSEDES", "DERIVED_FROM", "PROPAGATES_TO", "GOVERNED_BY", "CONSUMES_CAPABILITY", "USES_RUNTIME")


def _one(conn, q: str, args: tuple = ()):
    return conn.execute(q, args).fetchone()


def _name(conn, oid: str | None) -> str | None:
    r = _one(conn, "SELECT display_name FROM objects WHERE object_id = ?", (oid,)) if oid else None
    return r[0] if r else None


def _status(conn, oid: str) -> dict:
    r = _one(conn, "SELECT payload FROM aspects WHERE object_id = ? AND name = 'doc_status' AND is_current = 1", (oid,))
    return json.loads(r[0]) if r else {}


def _entity(conn, oid: str) -> dict:
    r = _one(conn, "SELECT payload FROM aspects WHERE object_id = ? AND name = 'entity' AND is_current = 1", (oid,))
    return json.loads(r[0]) if r else {}


def _edges(conn, scope: str, oid: str, rtype: str, direction: str = "out") -> list[str]:
    col, other = ("from_id", "to_id") if direction == "out" else ("to_id", "from_id")
    return [r[0] for r in conn.execute(
        f"SELECT {other} FROM relations WHERE {col} = ? AND relation_type = ? AND scope = ? AND expired_at IS NULL"
        " AND verification != 'DISPUTED'", (oid, rtype, scope))]


class Domains:
    """The imported Manifest domains, with the folder each one's index sits in."""

    def __init__(self, conn, scope: str):
        self.conn, self.scope = conn, scope
        self.by_id: dict[str, dict] = {}
        for oid, alias in conn.execute(
                "SELECT a.object_id, a.alias FROM entity_aliases a JOIN objects o ON o.object_id = a.object_id"
                " WHERE a.alias LIKE 'domain:%' AND o.scope = ?", (scope,)):
            desc = _entity(conn, oid).get("description") or ""
            m = re.search(r"current_owner_strategy=([A-Z_]+)", desc)
            idx = (_edges(conn, scope, oid, "INDEX_REF") or [None])[0]
            folder = (_edges(conn, scope, idx, "PART_OF") or [None])[0] if idx else None
            fname = _name(conn, folder) or ""
            if folder and re.match(r"^\s*00\b|START HERE", fname, re.I):
                # an index kept in the domain's 00 / START HERE sub-folder: the domain is the parent
                folder = (_edges(conn, scope, folder, "PART_OF") or [folder])[0]
            names = {alias[7:]}
            for seg in (_name(conn, idx) or "").split("｜"):
                seg = seg.strip()
                if len(seg) >= 2 and not re.fullmatch(r"\d+|索引|INDEX|00|README|START HERE", seg, re.I):
                    names.add(seg)
            self.by_id[oid] = {"domain": oid, "key": alias[7:], "name": _name(conn, oid), "index": idx,
                               "folder": folder, "strategy": m.group(1) if m else None, "names": names,
                               "parent": (_edges(conn, scope, oid, "BELONGS_TO") or [None])[0]}
        self.by_folder = {d["folder"]: d for d in self.by_id.values() if d["folder"]}

    def by_name(self, text: str) -> list[dict]:
        low = text.lower()
        hits = [d for d in self.by_id.values()
                if any(n.lower() == low or (len(n) >= 4 and re.search(r"(?<![\w])" + re.escape(n.lower()) + r"(?![\w])", low))
                       for n in d["names"])]
        # prefer the most specific (a subdomain over its parent)
        parents = {d["parent"] for d in hits}
        return [d for d in hits if d["domain"] not in parents]

    def of_object(self, oid: str) -> tuple[dict | None, str]:
        if oid in self.by_id:
            return self.by_id[oid], "the target is a domain"
        for d in _edges(self.conn, self.scope, oid, "BELONGS_TO"):
            if d in self.by_id:
                return self.by_id[d], "declared BELONGS_TO in the Pointer Manifest"
        seen, cur = set(), oid
        while cur and cur not in seen:
            seen.add(cur)
            if cur in self.by_folder and cur != oid:
                return self.by_folder[cur], f"folder tree: under {_name(self.conn, cur)!r} (the domain index's folder)"
            ups = _edges(self.conn, self.scope, cur, "PART_OF")
            cur = ups[0] if ups else None
        return None, "not under any domain index folder"


def _freshness(conn, oid: str | None, live_check: Callable | None) -> dict | None:
    if not oid:
        return None
    from .db import Store
    info = snapshot_info(Store(conn), oid)
    if info is None:
        native = _one(conn, "SELECT current_revision, updated_at FROM native_documents WHERE doc_id = ?", (oid,)) \
            if _one(conn, "SELECT 1 FROM sqlite_master WHERE name = 'native_documents'") else None
        return ({"kind": "nexus_native", "revision": native[0], "updated_at": native[1],
                 "note": "Nexus is this document's source of truth"} if native else None)
    info = with_live(info, live_check)
    return {"kind": "snapshot", "snapshot_version": info["snapshot_version"], "snapshot_taken": info["snapshot_taken"],
            "live": info["live"]["status"]}


def _by_declared_id(conn, scope: str, ident: str) -> list[str]:
    docs = []
    for oid, s, d in conn.execute(
            "SELECT o.object_id, o.statement, o.detail FROM observations o JOIN objects b ON b.object_id = o.object_id"
            " WHERE o.dimension = 'document_paragraph' AND b.scope = ? AND o.statement LIKE ?", (scope, f"%{ident}%")):
        anchor = json.loads(d or "{}").get("anchor") or ""
        if anchor.startswith("h0.") and re.search(r"(mission_id|program_id|contract_id|capability_id)\s*[:：]\s*"
                                                 + re.escape(ident), s):
            docs.append(oid)
    docs = list(dict.fromkeys(docs))
    typed = [o for o in docs if (_status(conn, o).get("object_type") or "").upper() in ("MISSION", "PROGRAM", "ROADMAP")]
    return typed or docs


def resolve(conn, scope: str, target: str, search: Callable[[str], list[str]] | None = None,
            live_check: Callable | None = None) -> dict:
    target = (target or "").strip()
    out: dict[str, Any] = {"target": target, "steps": []}
    step = out["steps"].append
    doms = Domains(conn, scope)
    r = _one(conn, "SELECT to_id FROM relations WHERE relation_type = 'ROOT_ENTRY_REF' AND scope = ? AND expired_at IS NULL",
             (scope,))
    root = r[0] if r else None
    step({"step": "READ_ROOT_ENTRY", "root": root, "manifest_domains": len(doms.by_id)})
    if not doms.by_id:
        out.update(state="NOT_CHECKED", note="no Pointer Manifest domains in this store (run nexus.ingest.manifest)")
        return out

    # 1. what is the target
    obj, domain, basis, conflicts = None, None, None, []
    if _one(conn, "SELECT 1 FROM objects WHERE object_id = ? AND scope = ?", (target, scope)):
        obj = target
    elif _ID.fullmatch(target):
        cands = _by_declared_id(conn, scope, target)
        if len(cands) > 1:
            conflicts.append({"kind": "ID_CLAIMED_BY_SEVERAL", "documents": cands})
        obj = cands[0] if cands else None
        basis = "declared id field in the document header" if cands else None
    if obj:
        domain, basis2 = doms.of_object(obj)
        basis = basis2 if obj == target else f"{basis}; {basis2}"
    else:
        named = doms.by_name(target)
        if len(named) == 1:
            domain, basis = named[0], "exact domain name"
        elif len(named) > 1:
            conflicts.append({"kind": "SEVERAL_DOMAINS_NAMED", "domains": [d["key"] for d in named]})
        elif search:
            # bounded discovery only proposes: a search hit never decides the domain (Manifest test E)
            cands = []
            for oid in search(target)[:5]:
                d, _b = doms.of_object(oid)
                cands.append({"object_id": oid, "name": _name(conn, oid), "domain": d and d["key"],
                              "status": _status(conn, oid).get("currency")})
            if cands:
                out["candidates"] = cands
    step({"step": "CLASSIFY_ROOT_OBJECT_OR_RETURN_UNKNOWN", "object": obj, "object_name": _name(conn, obj),
          "domain": domain and domain["key"], "basis": basis})
    if not obj and not domain:
        out.update(state="UNKNOWN",
                   note=("no object, declared id or domain name matched, so nothing is routed by guess. "
                         + ("Search candidates are listed (search creates no Authority): pick the one the "
                            "question is about and resolve its object_id." if out.get("candidates") else
                            "Use nexus_search, then resolve an object_id.")),
                   conflicts=conflicts or None)
        return out

    # 2. index and current owner of the domain
    owner_info = None
    if domain:
        owners = _edges(conn, scope, domain["domain"], "OWNS_CURRENT", "in")
        if len(owners) > 1:
            conflicts.append({"kind": "MULTIPLE_CURRENT_OWNERS", "domain": domain["key"], "owners": owners})
        desc = _entity(conn, domain["domain"]).get("description") or ""
        missing = re.findall(r"current_owner_ref=([\w-]+)", desc.split("NOT_IN_NEXUS", 1)[-1]) if "NOT_IN_NEXUS" in desc else []
        owner_info = {"domain": domain["key"], "domain_entity": domain["domain"], "index": domain["index"],
                      "index_name": _name(conn, domain["index"]), "strategy": domain["strategy"],
                      "strategy_meaning": STRATEGY_MEANING.get(domain["strategy"] or ""),
                      "current_owner": owners[0] if owners else None, "current_owner_name": _name(conn, owners[0]) if owners else None}
        if not owners:
            owner_info["current_owner_state"] = ("NOT_CHECKED: declared owner is not in Nexus (" + ", ".join(missing) + ")"
                                                 if missing else "NOT_CHECKED: no current owner declared")
        if domain.get("parent"):
            owner_info["parent_domain"] = doms.by_id.get(domain["parent"], {}).get("key")
        step({"step": "LOAD_EXACT_INDEX_REF", "index": domain["index"]})
    # 3. the most specific current: the target itself, its successor, or the domain owner
    specific = None
    if obj:
        st = _status(conn, obj)
        cur = st.get("currency")
        successors = _edges(conn, scope, obj, "SUPERSEDES", "in")
        if cur in RETIRED:
            out["target_state"] = "STALE"
            specific = successors[0] if successors else None
            out["target_note"] = (f"the target declares itself {cur}: history, not Current"
                                  + (f"; its successor is {specific}" if specific else "; no successor declared"))
        elif successors:
            conflicts.append({"kind": "CURRENT_BUT_SUPERSEDED", "document": obj, "superseded_by": successors})
        else:
            specific = obj
        if st:
            out["target_status"] = {k: st[k] for k in ("currency", "currency_basis", "object_type", "mission_state",
                                                       "plan_state", "authority_class") if st.get(k)}
    step({"step": "RESOLVE_MOST_SPECIFIC_CURRENT_OWNER", "most_specific": specific,
          "most_specific_name": _name(conn, specific), "domain_owner": owner_info and owner_info["current_owner"]})
    if owner_info:
        out["domain"] = owner_info
    out["most_specific"] = specific
    # 4. freshness
    fresh = {k: v for k, v in (("target", _freshness(conn, obj, live_check)),
                               ("most_specific", _freshness(conn, specific, live_check) if specific != obj else None),
                               ("domain_owner", _freshness(conn, owner_info and owner_info["current_owner"], live_check)))
             if v}
    out["freshness"] = fresh
    step({"step": "CHECK_FRESHNESS_AND_EXTERNAL_REALITY_REQUIREMENTS",
          "external_reality_first": bool(domain and domain["strategy"] == "EXTERNAL_REALITY_FIRST")})
    # 5. conflicts
    step({"step": "DETECT_CONFLICTS_BEFORE_MERGE", "conflicts": len(conflicts)})
    # 6. typed relationships and reverse impact (Manifest vocabulary)
    rels = []
    for focus in [x for x in (specific or obj,) if x]:
        for r in conn.execute(
                "SELECT relation_type, from_id, to_id, verification FROM relations WHERE (from_id = ? OR to_id = ?)"
                " AND scope = ? AND expired_at IS NULL AND verification != 'DISPUTED'", (focus, focus, scope)):
            if r[0] in MANIFEST_TYPES and r[0] != "PART_OF":
                rels.append({"type": r[0], "from": r[1], "to": r[2], "verification": r[3],
                             "other_name": _name(conn, r[2] if r[1] == focus else r[1])})
    out["relationships"] = rels[:20]
    out["reverse_impact"] = [x for x in rels if x["type"] == "DEPENDS_ON" and x["to"] == (specific or obj)]
    step({"step": "RESOLVE_TYPED_RELATIONSHIPS_AND_REVERSE_IMPACT", "relationships": len(rels)})
    # 7. bounded working set and authority
    spine = (_edges(conn, scope, domain["domain"], "GOVERNED_BY") or [None])[0] if domain else None
    ws = [x for x in dict.fromkeys([root, domain and domain["index"], specific,
                                    owner_info and owner_info["current_owner"], spine]) if x]
    out["working_set"] = [{"object_id": x, "name": _name(conn, x)} for x in ws]
    step({"step": "BUILD_BOUNDED_WORKING_SET", "size": len(ws)})
    out["authority"] = {
        "precedence": PRECEDENCE,
        "declared_authority_class": (out.get("target_status") or {}).get("authority_class"),
        "rule": ("Authority belongs to the declared owner of the fact (Source-Owned Authority), not to where the "
                 "text is stored or how it ranked; Nexus writes are reversible (REVERSIBLE_AUTONOMOUS); hard gates "
                 "(financial, security/identity, irreversible) are never inferred - see Global Semantic Spine §19."),
    }
    step({"step": "RESOLVE_AUTHORITY_BEFORE_ACTION"})
    # the domain's own owner pointer is a separate question from the target's
    if domain is None:
        out["domain_state"] = "UNKNOWN"
        out["note"] = ("the object is not under any domain the Pointer Manifest declares; its own status applies"
                       + (" - it is a Nexus-native document, so Nexus is its source of truth"
                          if (fresh.get("target") or {}).get("kind") == "nexus_native" else ""))
    elif owner_info and owner_info.get("current_owner") is None:
        out["domain_state"] = "NOT_CHECKED"
    else:
        out["domain_state"] = "KNOWN"
    if conflicts:
        out["conflicts"] = conflicts
        out["state"] = "CONFLICTED"
    elif out.get("target_state") == "STALE" and not specific:
        out["state"] = "STALE"
    elif obj:
        out["state"] = "KNOWN"           # the target and its most specific current are resolved
    else:
        out["state"] = out["domain_state"]
    return out
