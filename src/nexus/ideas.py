"""Ideas: principles the user wrote for their future self, recalled when a situation calls for them.

Retrieval answers what was asked; an idea should come up when the *situation* matches it, even if
nobody searched for it. This module

- imports the principle library (00｜原则库｜Messages to Self, a mirrored Drive document) as
  `idea` entities - one per numbered principle - with the user's own fields kept verbatim:
  title, Message (the opening lines), To State, For When, Effect, Origin, each with its anchor;
- keeps a short list of cue phrases per idea, derived by the agent from that idea's own
  For When / To State text (labelled as such; the user-authored trigger text is what is shown);
- `associate(text)` scores the ideas against a question or a document's opening text and returns
  at most two, with the For When line that matched - so search / read can attach them unasked.

Importance: every idea from the library is `core` (user-declared). The importer writes as
`import:principles` (the user's own declarations, VERIFIED with the library as provenance).
NEXUS_IDEAS=0 switches association off (ablation runs).
"""
from __future__ import annotations

import json
import os
import re

from .native import NativeDocs, _norm_alias

LIBRARY = "doc/1lbnkmJV-X03MOnE"
ACTOR = "import:principles"
MAX_IDEAS = 2

# Cue phrases per principle number, taken from (or closely paraphrasing) that principle's own
# For When / To State text. Agent-derived; the trigger text returned to readers is the user's.
CUES: dict[int, list[str]] = {
    1: ["不想动", "不想练", "不想去健身", "状态不好", "状态不对", "懒", "提不起劲", "开始训练", "要不要练", "健身房"],
    2: ["证明自己", "证明什么", "不够好", "验收", "像一场考试", "刚回伦敦", "开学前", "有没有成长"],
    3: ["深夜", "内耗", "情绪", "自我怀疑", "下结论", "今晚", "崩溃", "很难受"],
    4: ["休息", "罪恶感", "落后", "需要恢复", "停下来", "歇一天", "太累"],
    5: ["这说明什么", "说明我是", "自我审判", "求确认", "再测试一次", "反复确认自己", "念头"],
    6: ["中国留学生", "归成一类", "排斥", "比较", "同龄人", "和我很像"],
    7: ["联系方式", "要不要主动", "加微信", "搭话", "怕尴尬", "继续聊", "认识一下"],
    8: ["平台限制", "只能这样", "只能放弃", "绕路", "刻板印象", "砍了", "不支持", "一直不通", "走不通"],
    9: ["万一", "选错", "反复检查", "再确认", "再问一下", "保证", "遗漏", "例外", "重新考虑", "不放心", "担心"],
    10: ["再推进一点", "还想继续", "浪费机会", "不想停", "停下", "每天都更好", "再做一会", "上头"],
    11: ["教授进度", "教授已经讲到", "讲到 Lecture", "跟不上", "补上", "追上", "进度", "补课", "复习期"],
    12: ["普通的下午", "填满", "多做一点", "自由安排", "异步", "空闲", "今天是不是应该"],
    13: ["成绩", "分数", "0 分", "60 分", "高分", "冲分", "加码", "拿不到", "考不好", "Scenario"],
}
# ideas the user explicitly asked to be reminded of proactively (text of the principle says so)
PROACTIVE = {9, 13}


def enabled() -> bool:
    return os.environ.get("NEXUS_IDEAS", "1") != "0"


def _paras(conn, doc: str) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT statement, detail FROM observations WHERE object_id = ? AND"
                        " dimension = 'document_paragraph' ORDER BY observation_id", (doc,)).fetchall()
    return [(json.loads(d or "{}").get("anchor") or "", s or "") for s, d in rows]


def parse_library(paras: list[tuple[str, str]]) -> list[dict]:
    """Split the library into numbered principles; principle 01 is the untitled opening section."""
    groups: dict[int, list[tuple[str, str]]] = {}
    for a, t in paras:
        m = re.match(r"h(\d+)\.", a)
        if m:
            groups.setdefault(int(m.group(1)), []).append((a, t))
    out = []
    for sec, items in sorted(groups.items()):
        num = sec if sec > 0 else 1
        title_a, title = items[0]
        title = re.sub(r"^\d+｜", "", title.strip())
        fields, cur = {}, "Message" if sec == 0 else None
        for a, t in items[1:]:
            label = t.strip()
            if label in ("From State", "To State", "For When", "Message", "Effect", "Boundary"):
                cur = label
                continue
            if label.startswith(("Origin｜", "Supplement｜", "Echo Event｜")):
                cur = None
                if label.startswith("Origin｜"):
                    fields.setdefault("Origin", (a, label))
                continue
            if cur and cur not in fields:
                fields[cur] = (a, t.strip())
            elif cur == "Message" and len(fields["Message"][1]) < 120:
                fields["Message"] = (fields["Message"][0], fields["Message"][1] + " " + t.strip())
        out.append({"number": num, "title": title, "anchor": title_a,
                    **{k.lower().replace(" ", "_"): {"text": v[1], "anchor": v[0]} for k, v in fields.items()}})
    return out


def import_library(conn, scope: str, doc: str = LIBRARY) -> dict:
    from . import search as search_index
    n = NativeDocs(conn)
    fresh = n._index_fresh()
    stats = {"ideas": 0, "existing": 0}
    for p in parse_library(_paras(conn, doc)):
        alias = f"principle:{p['number']:02d}"
        hit = conn.execute("SELECT object_id FROM entity_aliases WHERE alias_norm = ? AND scope = ?",
                           (_norm_alias(alias), scope)).fetchone()
        if hit:
            stats["existing"] += 1
            continue
        msg = (p.get("message") or {}).get("text", "")
        when = (p.get("for_when") or {}).get("text", "")
        o = n.create_object(scope, "idea", f"原则 {p['number']:02d}｜{p['title']}", ACTOR,
                            aliases=[alias, f"原则 {p['number']:02d}", f"原则{p['number']}", p["title"]],
                            description=(f"{msg[:160]}" + (f" 〔何时想起：{when[:120]}〕" if when else "")),
                            evidence=[{"object_id": doc, "anchor": p["anchor"]}],
                            note="principle library import")
        oid = o["object_id"]
        payload = {"number": p["number"], "title": p["title"], "importance": "core (user-declared, 原则库)",
                   "proactive": p["number"] in PROACTIVE, "source": doc,
                   "fields": {k: v for k, v in p.items() if isinstance(v, dict)},
                   "cues": CUES.get(p["number"], []),
                   "cues_by": "agent:claude, from this principle's own For When / To State text"}
        conn.execute("INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle,"
                     " verification, revision, is_current, valid_from, updated_at) VALUES (?,?, 'idea', ?,"
                     " 'nexus-idea-1', 'CURRENT', 'VERIFIED', 1, 1, datetime('now'), datetime('now'))",
                     (f"{oid}:idea@1", oid, json.dumps(payload, ensure_ascii=False)))
        search_index.reindex_object(conn, oid)
        stats["ideas"] += 1
    if fresh:
        n._keep_fresh(True)
    conn.commit()
    return stats


def _load(conn, scope: str) -> list[dict]:
    rows = conn.execute("SELECT a.object_id, a.payload FROM aspects a JOIN objects o ON o.object_id = a.object_id"
                        " WHERE a.name = 'idea' AND a.is_current = 1 AND o.scope = ?", (scope,)).fetchall()
    return [{"object_id": r[0], **json.loads(r[1])} for r in rows]


def associate(conn, scope: str, text: str, limit: int = MAX_IDEAS) -> list[dict]:
    """Ideas whose situation matches `text` (a question, or a document's title and opening)."""
    if not enabled() or not (text or "").strip():
        return []
    low = text.lower()
    scored = []
    for idea in _load(conn, scope):
        hits = [c for c in idea.get("cues", []) if c.lower() in low]
        if not hits:
            continue
        score = len(set(hits)) + (0.5 if idea.get("proactive") else 0)
        scored.append((score, idea, hits))
    scored.sort(key=lambda x: (-x[0], x[1]["number"]))
    out = []
    for score, idea, hits in scored[:limit]:
        f = idea.get("fields", {})
        out.append({"idea": idea["object_id"], "title": f"原则 {idea['number']:02d}｜{idea['title']}",
                    "message": (f.get("message") or {}).get("text", "")[:200],
                    "for_when": (f.get("for_when") or {}).get("text", "")[:160],
                    "matched": hits[:4], "importance": idea.get("importance"),
                    "source": {"object_id": idea.get("source"), "anchor": (f.get("message") or {}).get("anchor")}})
    return out


def for_task(conn, scope: str, task: str, limit: int = MAX_IDEAS) -> list[dict]:
    """Ideas declared for a kind of task (payload ``"tasks": ["learning"]``), in full.

    `associate` depends on the wording of the question; a method the user wants followed for a
    whole kind of work must not. The entry call for that kind of work (nexus_learning_context for
    "learning") returns these first, whatever was asked and whether or not a course register exists.
    """
    if not enabled():
        return []
    out = []
    for idea in sorted(_load(conn, scope), key=lambda i: i.get("number", 0)):
        if task not in (idea.get("tasks") or []):
            continue
        f = idea.get("fields", {})
        item = {"idea": idea["object_id"], "title": idea.get("title"), "importance": idea.get("importance")}
        for k in ("message", "for_when", "boundary", "origin"):
            if (f.get(k) or {}).get("text"):
                item[k] = f[k]["text"]
        b = bound(conn, idea["object_id"], idea.get("bound_notes") or {})
        if b:
            item["bound"] = b
        if idea.get("links"):                    # files that are not objects here, e.g. a Drive PDF
            item["links"] = idea["links"]
        out.append(item)
    return out[:limit]


# Content bound to an idea: a relation from the content to the idea says what it does for it.
BOUND_ROLES = {"EXEMPLIFIES": "exemplar (a session where this was done right)",
               "CORRECTS": "correction (the user said what it is not)",
               "SUPPORTS": "source (where the user stated it)"}


def bind(conn, scope: str, idea_id: str, object_id: str, relation_type: str, actor: str = ACTOR) -> dict:
    """Bind one object to an idea (EXEMPLIFIES / CORRECTS / SUPPORTS); NativeDocs.remove_relation undoes it."""
    if relation_type not in BOUND_ROLES:
        raise ValueError(f"relation_type must be one of {sorted(BOUND_ROLES)}")
    return NativeDocs(conn).add_relation(scope, object_id, relation_type, idea_id, actor,
                                         note="bound to idea")


def source_url(conn, object_id: str) -> str | None:
    """Where the original of an object can be opened (a Drive link when it has one)."""
    for (payload,) in conn.execute("SELECT payload FROM aspects WHERE object_id = ? AND is_current = 1",
                                   (object_id,)):
        row = (json.loads(payload or "{}").get("row") or {})
        for k in ("source_url", "source_doc_url"):
            if str(row.get(k) or "").startswith("http"):
                return row[k]
    r = conn.execute("SELECT s.location FROM aspects a JOIN source_refs s ON s.source_ref_id = a.source_ref_id"
                     " WHERE a.object_id = ? AND a.is_current = 1 AND s.location LIKE 'http%' LIMIT 1",
                     (object_id,)).fetchone()
    if r:
        return r[0]
    r = conn.execute("SELECT source_ref_ids FROM observations WHERE object_id = ? AND source_ref_ids IS NOT NULL"
                     " LIMIT 1", (object_id,)).fetchone()
    for ref in (json.loads(r[0] or "[]") if r else []):
        loc = conn.execute("SELECT location FROM source_refs WHERE source_ref_id = ?", (ref,)).fetchone()
        if loc and str(loc[0] or "").startswith("http"):
            return loc[0]
    return None


def bound(conn, idea_id: str, notes: dict | None = None) -> list[dict]:
    """What is bound to this idea, with its role and a link to the original where there is one."""
    ph = ",".join("?" * len(BOUND_ROLES))
    out = []
    for oid, rtype, name in conn.execute(
            f"SELECT r.from_id, r.relation_type, o.display_name FROM relations r JOIN objects o"
            f" ON o.object_id = r.from_id WHERE r.to_id = ? AND r.relation_type IN ({ph})"
            f" AND r.expired_at IS NULL ORDER BY r.relation_type, r.created_at, r.relation_id",
            (idea_id, *BOUND_ROLES)):
        item = {"object_id": oid, "name": name, "role": BOUND_ROLES[rtype]}
        if (notes or {}).get(oid):
            item["note"] = notes[oid]
        url = source_url(conn, oid)
        if url:
            item["url"] = url
        out.append(item)
    return out


def add_idea(conn, scope: str, spec: dict, actor: str = "user") -> dict:
    """Write (or revise) one idea outside the principle library, with its task kinds and bound content.

    spec: {"key": "mother-problem", "number": 101, "title": ..., "fields": {"message": "...", "for_when": ...,
    "boundary": ..., "origin": ...}, "tasks": ["learning"], "cues": [...], "importance": "...",
    "aliases": [...], "evidence": {"object_id": ..., "anchor": ...}, "links": [{"title", "url"}],
    "bound": [{"object_id": ..., "relation": "EXEMPLIFIES", "note": "..."}]}
    Run again with the same key to revise the text (a new aspect revision) and add missing bindings.
    """
    from . import search as search_index
    n = NativeDocs(conn)
    fresh = n._index_fresh()
    ev = spec.get("evidence") or {}
    fields = {k: {"text": v, "anchor": ev.get("anchor")} for k, v in (spec.get("fields") or {}).items() if v}
    alias = f"idea:{spec['key']}"
    name = f"原则 {spec['number']}｜{spec['title']}"
    hit = conn.execute("SELECT object_id FROM entity_aliases WHERE alias_norm = ? AND scope = ?",
                       (_norm_alias(alias), scope)).fetchone()
    if hit:
        oid = hit[0]
        rev = conn.execute("SELECT MAX(revision) FROM aspects WHERE object_id = ? AND name = 'idea'",
                           (oid,)).fetchone()[0] + 1
        conn.execute("UPDATE aspects SET is_current = 0, valid_to = datetime('now') WHERE object_id = ?"
                     " AND name = 'idea' AND is_current = 1", (oid,))
        conn.execute("UPDATE objects SET display_name = ? WHERE object_id = ?", (name, oid))
    else:
        msg, when = (spec.get("fields") or {}).get("message", ""), (spec.get("fields") or {}).get("for_when", "")
        oid = n.create_object(scope, "idea", name, actor, aliases=[alias, *(spec.get("aliases") or [])],
                              description=msg[:160] + (f" 〔何时想起：{when[:120]}〕" if when else ""),
                              evidence=[ev] if ev.get("object_id") else None,
                              note="idea written on the user's instruction")["object_id"]
        rev = 1
    payload = {"number": spec["number"], "title": spec["title"], "importance": spec.get("importance"),
               "proactive": False, "source": ev.get("object_id"), "fields": fields,
               "cues": spec.get("cues") or [], "tasks": spec.get("tasks") or [],
               "bound_notes": {b["object_id"]: b["note"] for b in spec.get("bound") or [] if b.get("note")},
               "links": spec.get("links") or []}
    conn.execute("INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle,"
                 " verification, revision, is_current, valid_from, updated_at) VALUES (?,?, 'idea', ?,"
                 " 'nexus-idea-1', 'CURRENT', 'VERIFIED', ?, 1, datetime('now'), datetime('now'))",
                 (f"{oid}:idea@{rev}", oid, json.dumps(payload, ensure_ascii=False), rev))
    search_index.reindex_object(conn, oid)
    if fresh:
        n._keep_fresh(True)
    conn.commit()
    rels = []
    for b in spec.get("bound") or []:
        have = conn.execute("SELECT relation_id FROM relations WHERE from_id = ? AND to_id = ? AND relation_type = ?"
                            " AND expired_at IS NULL", (b["object_id"], oid, b["relation"])).fetchone()
        rels.append(have[0] if have else bind(conn, scope, oid, b["object_id"], b["relation"], actor)["relation_id"])
    conn.commit()
    return {"status": "OK", "idea": oid, "revision": rev, "relations": rels,
            "index_fresh": n._index_fresh()}


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .db import connect
    p = argparse.ArgumentParser(description="Add or revise an idea from a JSON spec; show what a task gets.")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("spec")
    a.add_argument("--actor", default="user")
    s = sub.add_parser("show")
    s.add_argument("task")
    args = p.parse_args(argv)
    conn = connect(args.db)
    if args.cmd == "add":
        with open(args.spec, encoding="utf-8") as f:
            out = add_idea(conn, args.scope, json.load(f), actor=args.actor)
    else:
        out = for_task(conn, args.scope, args.task)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


TASK_NOTE = ("how the user wants this kind of work done, written from sessions that went well. Plan from this "
             "before choosing materials; it outranks what is merely recent or plentiful. `bound` lists what "
             "belongs to it (exemplars, corrections, sources): nexus_read the object, or open its url (a Google "
             "Drive link reads the original file directly).")

IDEAS_NOTE = ("principles the user wrote for their future self whose 'for when' matches this situation; "
              "let them shape the answer where they fit, without quoting them every time")


if __name__ == "__main__":
    raise SystemExit(main())
