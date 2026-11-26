"""Build the Task 009 synthetic knowledge base.

One spec (``kb_spec.json``) is rendered two ways so the conditions compare like with like:

* plain documents (Root router, index, owner documents with noise, a historical tail and a
  draft section) for the Drive Fast Path B baseline;
* the same documents ingested into Nexus plus one structured ``knowledge`` object per item
  (statement / why / applies_when / not_when as an aspect, lifecycle, verification, validity,
  source section, and an authority binding).

Nothing here is real personal data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..db import Store, connect, init_schema
from ..ingest.pipeline import ingest_document
from ..ingest.sources import FetchedDocument

SOURCE_SYSTEM = "SYNTH_REPO"
EFFECTIVE_FROM = "2026-01-01T00:00:00Z"

FILLER = {
    "principles": [("例会节奏", "每周二 10:00 开例会，时长不超过 30 分钟，会后在群里发要点。"),
                   ("命名规范", "素材文件统一用“日期_主题_版本”命名，例如 20260912_片头_v2。")],
    "decisions": [("工具订阅", "项目管理工具沿用现有订阅，本季度不新增付费工具。"),
                  ("会议室", "线下会议优先使用共享办公室二楼的小会议室。")],
    "trial-report": [("试用背景", "试用的目的是检验省电管家原型在真实家庭中的可用性与稳定性。"),
                     ("设备清单", "每户安装 1 个主控盒和 3 个插座级传感器，由研究负责人统一发放。")],
    "data-handling": [("存储位置", "试用原始数据存放在工作室共享盘的受限目录，只有研究负责人可访问。"),
                      ("保留期限", "原始数据保留 12 个月后删除，统计汇总可长期保存。")],
}


def load_spec(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def item_marker(item: dict) -> str:
    return f"{item['title']}（{item['id']}）"


def render_docs(spec: dict) -> dict[str, str]:
    """Plain-text owner documents (headings are ``NN｜title`` lines, one paragraph per line),
    with noise sections, a draft section and a historical tail."""
    docs: dict[str, str] = {}
    for doc_id, meta in spec["docs"].items():
        lines = [meta["name"], f"Owner：{meta['owner']}　版本：{meta['version']}"]
        no = 0

        def head(title: str) -> None:
            nonlocal no
            no += 1
            lines.append(f"{no:02d}｜{title}")

        items = [i for i in spec["items"] if i["doc"] == doc_id]
        for item in (i for i in items if i["lifecycle"] == "CURRENT"):
            head(item_marker(item))
            lines += [item["statement"], f"原因：{item['why']}",
                      f"适用：{item['applies_when']}", f"不适用：{item['not_when']}"]
        for title, body in FILLER.get(doc_id, []):
            head(title)
            lines.append(body)
        for item in (i for i in items if i["lifecycle"] == "DRAFT"):
            head(item_marker(item))
            lines += [item["statement"], f"原因：{item['why']}"]
        for item in (i for i in items if i.get("supersedes")):
            sup = item["supersedes"]
            head(f"历史记录·{item['title']}（旧版，已被取代，仅供追溯）")
            lines.append(f"{sup['statement']}有效至 {sup['valid_to']}，已被上方现行决策取代。")
        docs[doc_id] = "\n".join(lines) + "\n"
    return docs


def render_router(spec: dict) -> dict[str, str]:
    """Root router and index used only by the baseline-B mirror (Drive-style layout)."""
    studio = spec["studio"]
    rows = "\n".join(f"- {meta['name']}｜文档 ID：{doc_id}｜Owner：{meta['owner']}"
                     for doc_id, meta in spec["docs"].items())
    root = (f"# {studio}｜Root Router\n"
            "本文件只负责指路：先看 Index 找到最具体的 Owner 文档，再读该文档。\n"
            "每份文档只有一个 Owner；“历史记录”和“提案（DRAFT）”不是现行规定。\n\n"
            "## 领域入口\n- 工作原则、决策、试用资料、数据处理：见 Index。\n")
    index = (f"# {studio}｜Index\n以下为最近一次整理的指针（不是权威内容，使用前请核对 Owner 文档）。\n\n"
             f"## 文档清单\n{rows}\n")
    return {"root": root, "index": index}


def _statement_anchor(sections: list[dict], match) -> str | None:
    """Anchor of the first paragraph (ordinal 1) under the first heading whose text matches."""
    heading = next((s for s in sections if s["heading"] and match(s["heading"])), None)
    if heading is None:
        return None
    ordered = sorted((s for s in sections if s["heading"] == heading["heading"]),
                     key=lambda s: s["ordinal"] if s["ordinal"] is not None else 0)
    return ordered[1]["anchor"] if len(ordered) > 1 else ordered[0]["anchor"]


def build_nexus(db_path: str | Path, spec: dict) -> dict[str, Any]:
    """Create/overwrite an isolated Nexus DB with the documents and structured knowledge objects."""
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    store = Store(connect(str(db_path)))
    init_schema(store.conn)
    scope = spec["scope"]
    store.add_scope(scope, "research", "synthetic Task 009 knowledge base")
    docs = render_docs(spec)
    refs: dict[str, dict] = {}
    for doc_id, text in docs.items():
        meta = spec["docs"][doc_id]
        fetched = FetchedDocument(SOURCE_SYSTEM, doc_id, meta["name"], meta["version"],
                                  "2026-10-01T00:00:00Z", None, text)
        res = ingest_document(store, fetched, scope, object_id=f"doc/{doc_id}", display_name=meta["name"])
        refs[doc_id] = res
    n = 0
    with store.tx():
        for item in spec["items"]:
            ref_id = refs[item["doc"]]["source_ref_id"]
            sections = store.get_sections(ref_id)
            anchor = _statement_anchor(sections, lambda h: f"（{item['id']}）" in h)
            if anchor is None:
                raise ValueError(f"no section for {item['id']}")
            oid = f"know/{item['id']}"
            store.add_object(oid, "knowledge", item_marker(item), scope)
            payload = {"id": item["id"], "kind": item["kind"], "statement": item["statement"],
                       "why": item["why"], "applies_when": item["applies_when"],
                       "not_when": item["not_when"], "triggers": item["triggers"],
                       "source": {"doc": item["doc"], "anchor": anchor, "source_ref_id": ref_id}}
            rev = 1
            if item.get("supersedes"):
                old = item["supersedes"]
                old_anchor = _statement_anchor(
                    sections, lambda h: h.startswith(tuple("0123456789")) and "历史记录·" in h
                    and item["title"] in h) or anchor
                store.add_aspect(f"{oid}#principle#1", oid, "principle",
                                 {**payload, "statement": old["statement"],
                                  "source": {**payload["source"], "anchor": old_anchor}},
                                 "t009.v1", "SUPERSEDED", "VERIFIED", 1, False,
                                 None, old["valid_to"], ref_id)
                rev = 2
            store.add_aspect(f"{oid}#principle#{rev}", oid, "principle", payload, "t009.v1",
                             item["lifecycle"], item["verification"], rev, True,
                             "2026-09-12T00:00:00Z" if item.get("supersedes") else EFFECTIVE_FROM,
                             None, ref_id)
            store.add_authority(f"auth/{item['id']}", "object", oid, None, item["owner"], scope,
                                f"{SOURCE_SYSTEM}:{item['doc']}", EFFECTIVE_FROM,
                                item["write_policy"], "NATIVE")
            n += 1
    return {"db": str(db_path), "docs": list(docs), "knowledge_objects": n}
