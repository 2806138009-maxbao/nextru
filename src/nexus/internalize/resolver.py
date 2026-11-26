"""Context resolver: match a task to long-term knowledge and return a Context Package.

The package carries what an agent needs to *judge* whether a record applies: the verbatim
statement, status (CURRENT / DRAFT), verification, validity, authority owner and write
policy, source anchor, and explicit applies_when / not_when. Matching is automatic from the
task text (no oracle): character-bigram overlap weighted by inverse document frequency, plus
trigger-word hits. It will return near misses on purpose-built negative tasks; that is the
point of measuring over-application.

Records that are SUPERSEDED are never returned as rules; a CURRENT record carries a note
about what it replaced. DRAFT records are returned flagged as not-a-decision. Experience
candidates (UNVERIFIED, proposal-only) are appended last, capped, and flagged.
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..db import Store

CANDIDATE_CAP = 2


def _bigrams(text: str) -> set[str]:
    t = "".join(ch for ch in text if not ch.isspace())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _payload(row: dict) -> dict:
    p = row["payload"]
    return json.loads(p) if isinstance(p, str) else p


class KnowledgeResolver:
    def __init__(self, store: Store, scope: str):
        self.store, self.scope = store, scope

    # -- loading ---------------------------------------------------------------------
    def _load(self, object_type: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT object_id FROM objects WHERE type = ? AND scope = ? ORDER BY object_id",
            (object_type, self.scope)).fetchall()
        out = []
        for (oid,) in rows:
            history = self.store.get_aspect_history(oid, "principle" if object_type == "knowledge"
                                                    else "experience")
            current = next((h for h in history if h["is_current"]), None)
            if current is None:
                continue
            binding = self.store.get_binding(oid, None, self.scope)
            older = [h for h in history if h["lifecycle"] == "SUPERSEDED"]
            out.append({"object_id": oid, "aspect": current, "payload": _payload(current),
                        "binding": binding, "superseded": [(_payload(o), o["valid_to"]) for o in older]})
        return out

    # -- scoring ---------------------------------------------------------------------
    @staticmethod
    def _doc_text(entry: dict) -> str:
        p = entry["payload"]
        return " ".join([p.get("title", ""), p["statement"], p["applies_when"],
                         " ".join(p.get("triggers", []))])

    def _score(self, task: str, entries: list[dict]) -> list[tuple[float, list[str], dict]]:
        query = _bigrams(task)
        docs = [_bigrams(self._doc_text(e)) for e in entries]
        n = max(len(docs), 1)
        df: dict[str, int] = {}
        for d in docs:
            for b in d:
                df[b] = df.get(b, 0) + 1
        scored = []
        for entry, d in zip(entries, docs):
            overlap = query & d
            idf_sum = sum(math.log(1 + n / df[b]) for b in overlap)
            hits = [t for t in entry["payload"].get("triggers", []) if t and t in task]
            score = idf_sum / math.sqrt(max(len(query), 1)) + 1.5 * len(hits)
            scored.append((score, hits, entry))
        return sorted(scored, key=lambda x: -x[0])

    # -- public ----------------------------------------------------------------------
    def _record(self, entry: dict, score: float, hits: list[str]) -> dict:
        p, b, a = entry["payload"], entry["binding"] or {}, entry["aspect"]
        rec = {
            "id": p["id"], "kind": p.get("kind"), "status": a["lifecycle"],
            "verification": a["verification"], "statement": p["statement"], "why": p.get("why"),
            "applies_when": p["applies_when"], "not_when": p["not_when"],
            "valid_from": a["valid_from"], "valid_to": a["valid_to"],
            "authority": {"owner": b.get("owner"), "write_policy": b.get("write_policy"),
                          "source_of_truth": b.get("source_of_truth")},
            "source": p.get("source"), "match": {"score": round(score, 2), "trigger_hits": hits},
        }
        if entry["superseded"]:
            old, valid_to = entry["superseded"][0]
            rec["supersedes"] = {"statement": old["statement"], "valid_to": valid_to}
        if a["lifecycle"] == "DRAFT" and p.get("kind") != "EXPERIENCE":
            rec["warning"] = "草案/提案，不是现行决定，不能当作既定事实或规则"
        return rec

    def resolve(self, task_text: str, k: int = 3, min_score: float = 1.0) -> dict[str, Any]:
        knowledge = [e for e in self._load("knowledge") if e["aspect"]["lifecycle"] in ("CURRENT", "DRAFT")]
        ranked = [(s, h, e) for s, h, e in self._score(task_text, knowledge) if s >= min_score][:k]
        items = [self._record(e, s, h) for s, h, e in ranked]
        # Retired (HISTORICAL) candidates are never resolved; promoted (CURRENT) ones would be
        # handled by a later gate, so only live DRAFT candidates are pushed here.
        experiences = [e for e in self._load("experience") if e["aspect"]["lifecycle"] == "DRAFT"]
        cand = [(s, h, e) for s, h, e in self._score(task_text, experiences) if s >= min_score][:CANDIDATE_CAP]
        candidates = []
        for s, h, e in cand:
            r = self._record(e, s, h)
            r["status"] = "UNVERIFIED_CANDIDATE"
            r["warning"] = "未经核验的经验候选：只是提示，不是事实或规则，不能覆盖上面的知识"
            candidates.append(r)
        return {"scope": self.scope, "task_chars": len(task_text), "items": items,
                "experience_candidates": candidates}

    def resolve_tiered(self, task_text: str, k: int = 3, min_score: float = 1.0,
                       rel_cut: float = 0.5) -> dict[str, Any]:
        """Same candidates as ``resolve`` (same top-k above ``min_score``), split into two tiers.

        ``items`` keeps records scoring at least ``rel_cut`` x the top score, in full. The rest go
        to ``weak_matches`` in a compact form (id, kind, status, statement, applies_when, source,
        score, warning). Nothing is dropped relative to ``resolve``: lexical scores cannot tell a
        single stray trigger hit from a weakly worded real match, so the weak tier trades package
        size for keeping recall. ``resolve`` itself is unchanged so Task 009 runs stay reproducible.
        """
        pkg = self.resolve(task_text, k=k, min_score=min_score)
        items = pkg["items"]
        if not items:
            return {**pkg, "weak_matches": [], "tiering": {"rel_cut": rel_cut, "top_score": 0.0}}
        top = items[0]["match"]["score"]
        strong = [r for r in items if r["match"]["score"] >= rel_cut * top]
        weak = [{"id": r["id"], "kind": r["kind"], "status": r["status"], "statement": r["statement"],
                 "applies_when": r["applies_when"], "source": r["source"], "match": r["match"],
                 **({"warning": r["warning"]} if r.get("warning") else {})}
                for r in items if r["match"]["score"] < rel_cut * top]
        return {**pkg, "items": strong, "weak_matches": weak,
                "tiering": {"rel_cut": rel_cut, "top_score": top}}

    def all_current(self) -> dict[str, Any]:
        """Everything that is CURRENT or DRAFT, unfiltered (the 'all resident' baseline)."""
        knowledge = [e for e in self._load("knowledge") if e["aspect"]["lifecycle"] in ("CURRENT", "DRAFT")]
        return {"scope": self.scope, "items": [self._record(e, 0.0, []) for e in knowledge],
                "experience_candidates": []}


def render_package(package: dict, header: str) -> str:
    lines = [header]
    for r in package["items"] + package["experience_candidates"]:
        a = r["authority"]
        src = r["source"] or {}
        lines.append(
            f"[{r['id']}] 状态：{r['status']}｜核验：{r['verification']}｜Authority：{a['owner']}"
            f"（{a['write_policy']}）｜来源：{src.get('doc')} · {src.get('anchor')}")
        lines.append(f"内容：{r['statement']}")
        if r.get("why"):
            lines.append(f"原因：{r['why']}")
        lines.append(f"适用：{r['applies_when']}　不适用：{r['not_when']}")
        if r.get("supersedes"):
            lines.append(f"（取代旧版：{r['supersedes']['statement']}，旧版有效至 {r['supersedes']['valid_to']}，已失效）")
        if r.get("warning"):
            lines.append(f"注意：{r['warning']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
