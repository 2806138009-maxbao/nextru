"""Relatedness spectrum: everything related to an object, in one list ordered by how far it can be
leaned on (planned 2026-11-11, nexus-web/docs/relatedness-spectrum.md).

Before this, relatedness came in two unconnected shapes - stored relations (typed, verified or not)
and `possibly_related` (title look-alikes computed on the fly) - and nothing said that a confirmed
edge and a two-character title overlap are not worth the same. The spectrum puts every related
object in exactly one tier, the strongest one it reaches:

  A  a verified relation that says something (EXEMPLIFIES, TEACHES, EVIDENCE_FOR ...)  usable as fact
  B  a verified filing or link (PART_OF, LINKS_TO, ...)                       usable as "where it sits"
  C  an agent's relation not yet verified; gate=True when it could change a decision, obligation or
     authority (read the quoted source before acting on it)                                   a lead
  D  structurally close: shares a parent or neighbours (siblings in one small collection)   a hint
  E  the text names it: real words of its title appear in this object's text               a hint

Read-only and computed; nothing here is stored. Within a tier, `score` only orders.

Tier E is stricter than `possibly_related` (which stays as it was): title bigrams that straddle a
word boundary (与当, 划与) or are generic (反馈, 阶段, 当前) no longer count, overlapping bigrams are
merged back into the words they came from, and a candidate needs one shared word of 4+ characters
or two shared words, one of them 3+ characters. `basis.words` shows those words.
"""

from __future__ import annotations

import json
import math
import sqlite3

from .linking import CJK_RUN, LATIN, _catalog, object_text, suggest, tokens
from .review import GATE, classify_relation

FILING_TYPES = {"PART_OF", "BELONGS_TO", "ABOUT", "RECORDED_IN", "LISTED_IN", "LINKS_TO", "INDEX_REF",
                "REGISTER_OF"}
TIER_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
CAPS = {"A": 50, "B": 20, "C": 30, "D": 5, "E": 5}
D_MAX_DEGREE = 40          # a parent with more members than this says little about any two of them
D_MIN_SCORE = 0.4          # one shared neighbour with <= 11 members, or several looser ones
RARE_LATIN_IDF = 5.0       # an English word in fewer than ~1% of titles (MOSFET) counts as a full word
E_POOL = 25                # text candidates examined before the stricter word filter
EDGE_BASIS = 4             # relations listed per object in `basis`

# characters that make a bigram straddle words (与当前 -> 与当) or a number word (一道, 两条, 第三)
FUNCTION_CHARS = set("与和的了是在及或对为之而把被将从以于并也就都所其这那个些一二两三四五六七八九十第各每")
GENERIC = frozenset(
    "反馈 阶段 当前 评估 能力 个人 方向 研究 计划 体系 课程 提出 工作 作流 流程 候选 方案 系统 框架 "
    "版本 结构 问题 方法 学习 项目 自己 真实 世界 进行 中的 内容 整理 状态 说明 规则 基础 结果 分析 "
    "设计 总结 更新 资料 文档 相关 关系 信息 数据 过程 目标 管理 使用 时间 今天 当日 已经 现在".split())


def _edges(conn: sqlite3.Connection, object_id: str, scope: str) -> list[dict]:
    rows = conn.execute(
        "SELECT relation_id, from_id, relation_type, to_id, verification, provenance_refs, created_at"
        " FROM relations WHERE expired_at IS NULL AND scope = ? AND (from_id = ? OR to_id = ?)",
        (scope, object_id, object_id)).fetchall()
    gate_ids = set()
    ids = [r["relation_id"] for r in rows]
    if ids:
        try:
            gate_ids = {r[0] for r in conn.execute(
                "SELECT relation_id FROM native_proposals WHERE kind = 'RELATION'"
                " AND json_extract(payload, '$.review') = 'GATE'"
                f" AND relation_id IN ({','.join('?' * len(ids))})", ids)}
        except sqlite3.Error:
            pass
    out = []
    for r in rows:
        if r["from_id"] == r["to_id"]:
            continue
        other = r["to_id"] if r["from_id"] == object_id else r["from_id"]
        try:
            refs = json.loads(r["provenance_refs"] or "[]")
        except ValueError:
            refs = []
        verified = r["verification"] == "VERIFIED"
        typ = r["relation_type"]
        if verified:
            tier = "B" if typ.upper() in FILING_TYPES or typ.upper().endswith("_REF") else "A"
            gate = False
        else:
            tier = "C"
            gate = r["relation_id"] in gate_ids or classify_relation(
                conn, r["from_id"], typ, r["to_id"], refs)["tier"] == GATE
        out.append({"other": other, "tier": tier, "gate": gate, "created_at": r["created_at"] or "",
                    "refs": len(refs),
                    "edge": {"relation_id": r["relation_id"], "type": typ,
                             "direction": "out" if r["from_id"] == object_id else "in",
                             "verification": r["verification"]} | ({"check_source": True} if gate else {})})
    return out


def _structural(conn: sqlite3.Connection, object_id: str, scope: str, direct: set[str]) -> dict[str, dict]:
    """Objects sharing a neighbour with this one, weighted Adamic-Adar style: a shared parent of 5
    members counts far more than one of 40; parents larger than D_MAX_DEGREE are ignored."""
    nbrs = {r[0] for r in conn.execute(
        "SELECT CASE WHEN from_id = ? THEN to_id ELSE from_id END FROM relations"
        " WHERE expired_at IS NULL AND scope = ? AND (from_id = ? OR to_id = ?)",
        (object_id, scope, object_id, object_id))}
    out: dict[str, dict] = {}
    for n in nbrs:
        members = {r[0] for r in conn.execute(
            "SELECT CASE WHEN from_id = ? THEN to_id ELSE from_id END FROM relations"
            " WHERE expired_at IS NULL AND scope = ? AND (from_id = ? OR to_id = ?)",
            (n, scope, n, n))}
        if len(members) > D_MAX_DEGREE or len(members) < 2:
            continue
        w = 1.0 / math.log(1 + len(members))
        for m in members - {object_id} - direct:
            e = out.setdefault(m, {"score": 0.0, "via": []})
            e["score"] += w
            e["via"].append(n)
    return out


def shared_words(title: str, shared: set[str], idf: dict[str, float] | None = None) -> list[tuple[str, int]]:
    """Merge the shared bigrams back into the words of `title` they came from (健身+身动+动作 ->
    健身动作) -> [(word, weight)]. Bigrams with function characters (与当, 一道) are dropped first;
    a word made only of generic parts (真实世界反馈) is dropped after merging. An English word weighs
    3 when rare across titles (MOSFET), 2 when common (Google, Topic)."""
    keep = {t for t in shared if not (set(t) & FUNCTION_CHARS)}
    words: list[tuple[str, int]] = []
    for run in CJK_RUN.findall(title or ""):
        i = 0
        while i < len(run) - 1:
            if run[i:i + 2] in keep:
                j = i + 1
                while j < len(run) - 1 and run[j:j + 2] in keep:
                    j += 1
                w = run[i:j + 1]
                words.append((w, _cjk_weight(w)))
                i = j + 1
            else:
                i += 1
    for w in LATIN.findall(title or ""):
        lw = w.lower().strip(".-+")
        if lw in keep and len(lw) >= 4:
            words.append((w, 3 if idf is None or idf.get(lw, 0) >= RARE_LATIN_IDF else 2))
    seen, out = set(), []
    for w, n in words:
        if w.lower() not in seen and n >= 2:
            seen.add(w.lower())
            out.append((w, n))
    return out


def _cjk_weight(w: str) -> int:
    """Characters of a word that carry its topic: those covered by a non-generic bigram that has at
    least one character outside the generic words (so 实世 in 真实世界 does not count)."""
    generic_cover = set()
    for i in range(len(w) - 1):
        if w[i:i + 2] in GENERIC:
            generic_cover.update((i, i + 1))
    content = set()
    for i in range(len(w) - 1):
        if w[i:i + 2] not in GENERIC and not {i, i + 1} <= generic_cover:
            content.update((i, i + 1))
    return len(content)


def strong_enough(words: list[tuple[str, int]]) -> bool:
    """One word of 4+ topic characters, or two words with the longer one 3+."""
    lens = sorted((n for _, n in words), reverse=True)
    return bool(lens) and (lens[0] >= 4 or (len(lens) >= 2 and lens[0] >= 3))


def spectrum(conn: sqlite3.Connection, object_id: str, scope: str, text: str | None = None) -> dict:
    """{"items": [...strongest first], "counts": {tier: total before caps}}."""
    names = {}

    def info(oid: str) -> tuple[str | None, str | None]:
        if oid not in names:
            r = conn.execute("SELECT display_name, type FROM objects WHERE object_id = ?", (oid,)).fetchone()
            names[oid] = (r[0], r[1]) if r else (None, None)
        return names[oid]

    archived = set()
    try:
        archived = {r[0] for r in conn.execute(
            "SELECT doc_id FROM native_documents WHERE archived_at IS NOT NULL")}
    except sqlite3.Error:
        pass

    best: dict[str, dict] = {}
    for e in _edges(conn, object_id, scope):
        oid = e["other"]
        if oid in archived:
            continue
        cur = best.get(oid)
        rank = (TIER_ORDER[e["tier"]], 0 if e["gate"] else 1)
        if cur is None:
            best[oid] = cur = {"object_id": oid, "tier": e["tier"], "gate": e["gate"], "_rank": rank,
                               "_edges": [], "_latest": "", "_refs": 0}
        elif rank < cur["_rank"]:
            cur.update(tier=e["tier"], gate=e["gate"], _rank=rank)
        cur["_edges"].append(e["edge"])
        cur["_latest"] = max(cur["_latest"], e["created_at"])
        cur["_refs"] += e["refs"]
    for v in best.values():
        if v["tier"] in ("A", "B"):
            v["score"] = len(v["_edges"])
        else:
            v["score"] = v["_refs"]
        v["basis"] = {"relations": v["_edges"][:EDGE_BASIS]}
        if len(v["_edges"]) > EDGE_BASIS:
            v["basis"]["more_relations"] = len(v["_edges"]) - EDGE_BASIS

    direct = set(best)
    for oid, s in _structural(conn, object_id, scope, direct).items():
        typ = info(oid)[1]
        if s["score"] < D_MIN_SCORE or oid in archived or typ in (None, "aspect", "scope", "folder"):
            continue
        best[oid] = {"object_id": oid, "tier": "D", "gate": False, "_rank": (3, 1), "_latest": "",
                     "score": round(s["score"], 2),
                     "basis": {"shared_neighbours": [{"object_id": n, "name": info(n)[0]}
                                                     for n in s["via"][:3]]}}

    cat = _catalog(conn, scope)
    full = tokens(text if text is not None else object_text(conn, object_id))
    for m in suggest(conn, object_id, scope, limit=E_POOL, text=text):
        oid = m["object_id"]
        if oid in best or oid in archived:
            continue
        o = cat.objects.get(m.get("via_folder") or oid) or cat.objects.get(oid)
        words = shared_words(o["name"], o["t"] & full, cat.idf) if o else []
        if not strong_enough(words):
            continue
        best[oid] = {"object_id": oid, "tier": "E", "gate": False, "_rank": (4, 1), "_latest": "",
                     "score": m["score"], "basis": {"words": [w for w, _ in words]}}

    counts = {t: 0 for t in TIER_ORDER}
    for v in best.values():
        counts[v["tier"]] += 1
    ordered = sorted(best.values(), key=lambda v: (v["_rank"], -float(v["score"]),
                                                   _neg(v.get("_latest", "")), v["object_id"]))
    taken = {t: 0 for t in TIER_ORDER}
    items = []
    for v in ordered:
        if taken[v["tier"]] >= CAPS[v["tier"]]:
            continue
        taken[v["tier"]] += 1
        name, typ = info(v["object_id"])
        item = {"object_id": v["object_id"], "name": name, "type": typ, "tier": v["tier"],
                "score": v["score"], "basis": v["basis"]}
        if v["gate"]:
            item["gate"] = True
        items.append(item)
    return {"items": items, "counts": counts}


def _neg(s: str) -> str:
    """Sort key that puts later timestamps first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in s) if s else "\U0010FFFF"


SPECTRUM_NOTE = (
    "spectrum: every related object once, strongest tier first. A = verified relation with meaning "
    "and B = verified filing/link: usable as fact (B only for where it sits). C = an agent's "
    "unverified relation, a lead; gate=true means it could change a decision, obligation or "
    "authority - read the quoted source before acting on it. D = structurally close (shares a small "
    "parent or neighbours) and E = the text names its title (basis.words): hints only, read before "
    "citing; never add a relation just because something is in D or E. counts = totals before caps.")
