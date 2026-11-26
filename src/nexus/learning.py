"""One-call learning context over an imported course CORE REGISTER (``nexus.ingest.register``).

``learning_context(store, scope, topic)`` resolves a topic ("零 ISI", "Nyquist", "shape", "Ex4") to
the course knowledge node(s) it is about, using only the register's own rows, and returns in one
bundle what a tutor needs before a lesson:

* the node and its Learner_State Current (state, last observed, boundary, next evidence gate);
* prerequisites (incoming PREREQUISITE_OF) with their own states, and where it leads (TRANSFERS_TO);
* the teaching materials pointing at it (questions / examples / resources with the relation, the
  Owner's coverage text such as DIRECT / PARTIAL / NOT_COVERED, and the relation rationale);
* the latest learner receipts for the node, newest first;
* the register snapshot version and whether the Sheet still has it.

Everything is read from rows the Owner wrote; nothing is inferred, ranked by importance or
promoted. Each item carries its anchor in the register snapshot so it can be quoted or traced.
An empty topic lists every knowledge node with its state (a "where am I" overview).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .db import Store
from .freshness import snapshot_info, with_live
from .ingest.register import SCHEMA, _slug
from .search import query_groups

MATERIAL_TYPES = ("PRACTISES", "EXPLAINS", "TESTS", "WORKED_EXAMPLE_OF", "TEACHES")
STATE_FIELDS = ("capability_state", "last_observed_date", "latest_receipt_id", "state_delta",
                "reconciled_learning_evidence_boundary", "failure_pattern", "next_independent_evidence_gate",
                "record_status")
ROLE_FIELDS = ("canonical_label", "topic_ref", "learning_role", "processing_state", "fresh_depth",
               "evidence_depth", "epistemic_state", "source_url")
CLIP = 700


def _clip(v: Any, n: int = CLIP) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"…（截断，全文见 anchor，共 {len(v)} 字）"
    return v


class _Reg:
    def __init__(self, store: Store, scope: str, course: str | None):
        c = store.conn
        rows = c.execute("SELECT o.object_id, a.payload FROM objects o JOIN aspects a ON a.object_id = o.object_id"
                         " AND a.name = 'register_import' AND a.is_current = 1 AND a.schema_version = ?"
                         " WHERE o.type = 'course' AND o.scope = ?", (SCHEMA, scope)).fetchall()
        if course:
            rows = [r for r in rows if r["object_id"] == f"reg/{course.lower()}"]
        self.courses = [(r["object_id"], json.loads(r["payload"])) for r in rows]
        self.c = c

    def aspect(self, oid: str, name: str) -> dict | None:
        r = self.c.execute("SELECT payload FROM aspects WHERE object_id = ? AND name = ? AND is_current = 1"
                           " AND schema_version = ?", (oid, name, SCHEMA)).fetchone()
        return json.loads(r["payload"]) if r else None

    def obj(self, oid: str):
        return self.c.execute("SELECT object_id, type, display_name FROM objects WHERE object_id = ?",
                              (oid,)).fetchone()

    def edges(self, oid: str, direction: str, types: tuple[str, ...]):
        col, other = ("to_id", "from_id") if direction == "in" else ("from_id", "to_id")
        ph = ",".join("?" * len(types))
        return self.c.execute(f"SELECT relation_id, from_id, relation_type, to_id FROM relations"
                              f" WHERE {col} = ? AND relation_type IN ({ph}) ORDER BY relation_id",
                              (oid, *types)).fetchall()

    def edge_note(self, rid: str) -> dict:
        r = self.c.execute("SELECT statement, detail FROM observations WHERE dimension = 'relation_evidence'"
                           " AND detail LIKE ?", (f'%"relation_id": "{rid}"%',)).fetchone()
        if not r:
            return {}
        d = json.loads(r["detail"])
        return {k: d[k] for k in ("anchor", "support_type", "epistemic_state", "rationale") if d.get(k)}


def _state(reg: _Reg, oid: str) -> dict | None:
    a = reg.aspect(oid, "learner_state")
    if not a:
        return None
    row = a.get("row") or {}
    out = {k: _clip(row[k]) for k in STATE_FIELDS if row.get(k)}
    out["anchor"] = a.get("anchor")
    r = reg.c.execute("SELECT revision FROM aspects WHERE object_id = ? AND name = 'learner_state' AND is_current = 1",
                      (oid,)).fetchone()
    out["revision"] = r[0] if r else None       # pass as expected_revision to nexus_register_edit
    return out


def _brief_state(reg: _Reg, oid: str) -> str | None:
    a = reg.aspect(oid, "learner_state")
    return (a.get("row") or {}).get("capability_state") if a else None


def _resolve(reg: _Reg, prefix: str, ref: str, topic: str) -> tuple[list[tuple[str, int]], list[dict]]:
    groups = query_groups(topic)
    exact = f"{prefix}/{_slug(topic)}"
    o = reg.obj(exact)
    if o is not None and o["type"] == "knowledge":
        return [(exact, 10 ** 6)], []
    scores: dict[str, int] = {}
    matched: list[dict] = []
    for r in reg.c.execute("SELECT anchor, body FROM source_sections WHERE source_ref_id = ?", (ref,)):
        body = r["body"]
        low = body.lower()
        score = sum(max((len(t) for t in g if t.lower() in low), default=0) for g in groups)
        if not score:
            continue
        local = body.removeprefix("· ").split(" | ", 1)[0].strip()
        targets: list[str] = []
        for cand in (f"{prefix}/{_slug(local)}", f"{prefix}/receipt/{_slug(local)}"):
            ob = reg.obj(cand)
            if ob is None:
                continue
            if ob["type"] == "knowledge":
                targets.append(cand)
            else:
                targets += [e["to_id"] for e in reg.edges(cand, "out", MATERIAL_TYPES + ("EVIDENCE_FOR",))
                            if (reg.obj(e["to_id"]) or {"type": ""})["type"] == "knowledge"]
        rel = reg.c.execute("SELECT from_id, to_id FROM relations WHERE relation_id = ?",
                            (f"{prefix}/{_slug(local)}",)).fetchone()
        if rel:
            targets += [x for x in (rel["from_id"], rel["to_id"])
                        if (reg.obj(x) or {"type": ""})["type"] == "knowledge"]
        for t in set(targets):
            scores[t] = scores.get(t, 0) + score
        if targets:
            matched.append({"anchor": r["anchor"], "score": score, "row": _clip(body, 220)})
    matched.sort(key=lambda m: -m["score"])
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])), matched[:6]


def learning_context(store: Store, scope: str, topic: str = "", course: str | None = None,
                     max_receipts: int = 5, live_check: Callable | None = None) -> dict[str, Any]:
    reg = _Reg(store, scope, course)
    if not reg.courses:
        return {"status": "NOT_FOUND", "note": "no imported course register in this scope"
                + (f" for course {course}" if course else "")}
    if len(reg.courses) > 1 and not course and topic.strip():
        # pick the course whose register matches the topic best
        best = []
        for oid, meta in reg.courses:
            ranked, _ = _resolve(reg, oid, meta["source_ref_id"], topic)
            best.append((ranked[0][1] if ranked else 0, oid, meta))
        best.sort(key=lambda x: -x[0])
        reg.courses = [(best[0][1], best[0][2])]
    prefix, meta = reg.courses[0]
    ref = meta["source_ref_id"]
    doc_oid = reg.c.execute("SELECT from_id FROM relations WHERE relation_id = ?",
                            (f"{prefix}/register",)).fetchone()
    info = snapshot_info(store, doc_oid["from_id"]) if doc_oid else None
    register = {"course": prefix, "name": meta.get("register_name"), "source_ref_id": ref,
                "snapshot_modified": meta.get("modified_time")}
    moved = reg.c.execute("SELECT effective_from FROM authority_bindings WHERE target_id = ? AND field_scope"
                          " IS NULL AND effective_to IS NULL AND migration_state = 'MIGRATED'", (prefix,)).fetchone()
    if moved:
        register.update(source_of_truth="NEXUS", migrated_at=moved[0],
                        sheet="pre-cutover archive (not imported again)")
        if info:
            register.update(snapshot_version=info.get("snapshot_version"))
        note = ("This course register lives in Nexus (moved from its Google Sheet at the cutover); rows are "
                "edited here, the Sheet is the pre-cutover archive. Learner_State is the only formal learning "
                "Current; coverage text (DIRECT / PARTIAL / NOT_COVERED) and relation rationale are the Owner's "
                "own boundaries. Rows added or edited after the cutover have no Sheet anchor; quote them by "
                "object id (nexus_read).")
    else:
        if info:
            info = with_live(info, live_check)
            register.update(snapshot_version=info.get("snapshot_version"), live=info.get("live"))
        note = ("All content is the course Owner register as written (read-only mirror). Learner_State is the "
                "only formal learning Current; coverage text (DIRECT / PARTIAL / NOT_COVERED) and relation "
                "rationale are the Owner's own boundaries. Quote by anchor; nexus_read(object_id) for full rows.")

    if not topic.strip():
        nodes = []
        for r in reg.c.execute("SELECT object_id, display_name FROM objects WHERE type = 'knowledge'"
                               " AND object_id LIKE ? ORDER BY object_id", (prefix + "/%",)):
            st = _state(reg, r["object_id"]) or {}
            nodes.append({"id": r["object_id"], "label": r["display_name"],
                          **{k: st.get(k) for k in ("capability_state", "last_observed_date", "anchor")}})
        return {"status": "OK", "register": register, "knowledge_nodes": nodes, "note": note}

    ranked, matched = _resolve(reg, prefix, ref, topic)
    if not ranked:
        return {"status": "NOT_FOUND", "register": register,
                "note": f"no register row mentions {topic!r}; try nexus_search for documents"}
    node = ranked[0][0]
    o = reg.obj(node)
    prereq = [{"id": e["from_id"], "label": (reg.obj(e["from_id"]) or {"display_name": None})["display_name"],
               "capability_state": _brief_state(reg, e["from_id"]), "relation": e["relation_id"],
               **reg.edge_note(e["relation_id"])}
              for e in reg.edges(node, "in", ("PREREQUISITE_OF",))]
    leads = [{"id": e["to_id"], "relation": e["relation_id"], "capability_state": _brief_state(reg, e["to_id"]),
              **reg.edge_note(e["relation_id"])} for e in reg.edges(node, "out", ("TRANSFERS_TO",))]
    materials = []
    for e in reg.edges(node, "in", MATERIAL_TYPES):
        m = reg.obj(e["from_id"])
        row = (reg.aspect(e["from_id"], "register_row") or {})
        materials.append({"id": e["from_id"], "type": m["type"] if m else None, "relation": e["relation_type"],
                          "edge": e["relation_id"],
                          **{k: _clip(v, 400) for k, v in (row.get("row") or {}).items() if k in ROLE_FIELDS},
                          "anchor": row.get("anchor"), "edge_note": reg.edge_note(e["relation_id"])})
    receipts = []
    for e in reg.edges(node, "in", ("EVIDENCE_FOR",)):
        a = reg.aspect(e["from_id"], "receipt") or {}
        row = a.get("row") or {}
        text = next((v for k, v in row.items() if k.startswith("verbatim")), "")
        receipts.append({"id": e["from_id"], "date": row.get("date"), "evidence_class": row.get("evidence_class"),
                         "knowledge_ids": row.get("knowledge_ids"), "excerpt": _clip(text, 600),
                         "anchor": a.get("anchor")})
    receipts.sort(key=lambda r: r.get("date") or "", reverse=True)
    others = [{"id": k, "score": s, "capability_state": _brief_state(reg, k)} for k, s in ranked[1:4]]
    return {"status": "OK", "register": register, "topic": topic,
            "node": {"id": node, "label": o["display_name"] if o else None, "learner_state": _state(reg, node)},
            "prerequisites": prereq, "leads_to": leads, "materials": materials,
            "receipts": receipts[:max_receipts], "receipts_total": len(receipts),
            "other_matching_nodes": others, "matched_rows": matched, "note": note}
