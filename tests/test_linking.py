"""Link suggestions (nexus.linking): writes return suggested_links, nexus_related lists possibly_related.

The case behind it (2026-11-10): a cross-domain learning entry was filed with one PART_OF and no link
to the 自媒体 research and Opportunity Radar documents it builds on.
"""

from __future__ import annotations

import asyncio
import json

from helpers import NexusTest

from nexus.agent_tools import AgentTools
from nexus.linking import suggest, tokens
from nexus.mcp_server import build_server
from nexus.native import NativeDocs

SCOPE = "research:links"
ENTRY = {"title": "学习入口｜产品运营 × 商业分析 × 创业金融",
         "children": [{"kind": "section", "title": "创作者经济与内容商业",
                       "children": ["围绕 UP 主和自媒体创作研究选题、商业化与受众；不取代 Opportunity Radar 里关于创作者与市场的研究方向。"
                                    " 参考 https://example.com/make-changes/share-structure"]}]}


def call(server, tool, args) -> dict:
    res = asyncio.run(server.call_tool(tool, args))
    blocks = res[0] if isinstance(res, tuple) else res
    blocks = getattr(blocks, "content", blocks)
    return json.loads("".join(getattr(b, "text", "") for b in blocks))


class LinkingTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        st = self.store
        with st.tx():
            st.add_scope(SCOPE, "research", "link suggestion test")
            for oid, typ, name in (
                    ("doc/media", "document", "01｜LIVING｜自媒体创作研究｜表达 × 受众 × 商业化｜讨论笔记"),
                    ("doc/radar", "document", "00｜PROJECT｜Opportunity Radar｜研究方向 × Signals"),
                    ("ent/folder/radar", "folder", "Opportunity Radar｜创作者 × 市场"),
                    ("ent/folder/misc", "folder", "研究案例 05｜持续运营"),
                    ("doc/s1", "document", "001-A｜社会经验研究｜绩点与就业竞争力｜冤种小徐"),
                    ("doc/s2", "document", "001-B｜社会经验研究｜实习的真实价值｜冤种小徐"),
                    ("ent/folder/xu", "folder", "冤种小徐"),
                    ("doc/structure", "document", "Structure｜Year 2 Course Baseline"),
                    ("doc/game", "document", "明日方舟｜卫戍协议阵容 01")):
                st.add_object(oid, typ, name, SCOPE)
            for i, filler in enumerate(("DEMO0020 Lecture", "游戏攻略 影之刃零", "家庭人物主档", "Year 1 回顾",
                                        "原神长期数据库", "健身计划 v0.1", "LinkedIn 简历", "宿舍关系史",
                                        "Multisim 技术评审", "Drive 总索引", "Persona London 前传",
                                        "原则库 Messages to Self", "EXU Attendance", "Scenario Y DPS",
                                        "Semantic Spine 标准", "Pointer Manifest 合同", "伦敦生活模型",
                                        "收件箱", "饮食与餐饮记录", "训练记录") * 2):
                st.add_object(f"doc/filler{i}", "document", f"{filler} {i}", SCOPE)
            st.add_relation("rel/r1", "doc/radar", "PART_OF", "ent/folder/radar", "VERIFIED", SCOPE, [])
            st.add_relation("rel/s1", "doc/s1", "PART_OF", "ent/folder/xu", "VERIFIED", SCOPE, [])
            st.add_relation("rel/s2", "doc/s2", "PART_OF", "ent/folder/xu", "VERIFIED", SCOPE, [])
        self.conn.commit()
        self.n = NativeDocs(self.conn, agent_writes=True)

    def ids(self, doc_id: str, **kw) -> list[str]:
        return [s["object_id"] for s in suggest(self.conn, doc_id, SCOPE, **kw)]

    def test_tokens_drop_urls_and_filler(self) -> None:
        t = tokens("01｜LIVING｜自媒体 https://x.org/share-structure 归档副本 Opportunity v0.1")
        self.assertIn("自媒", t)
        self.assertIn("opportunity", t)
        self.assertFalse({"living", "share-structure", "structure", "归档", "v0.1"} & t)

    def test_entry_finds_what_it_builds_on(self) -> None:
        doc = self.n.create(SCOPE, ENTRY, "agent:chatgpt")["doc_id"]
        got = self.ids(doc)
        self.assertEqual(set(got[:2]), {"doc/media", "doc/radar"})
        self.assertNotIn("doc/structure", got)          # a word inside a URL is not a mention
        self.assertNotIn("doc/game", got)

    def test_folder_becomes_its_index_document_and_bare_folders_are_dropped(self) -> None:
        doc = self.n.create(SCOPE, ENTRY, "agent:chatgpt")["doc_id"]
        s = suggest(self.conn, doc, SCOPE)
        radar = [x for x in s if x["object_id"] == "doc/radar"]
        self.assertEqual(len(radar), 1)
        self.assertFalse(any(x["type"] == "folder" for x in s))

    def test_already_linked_objects_are_left_out(self) -> None:
        doc = self.n.create(SCOPE, ENTRY, "agent:chatgpt")["doc_id"]
        self.n.add_relation(SCOPE, doc, "LINKS_TO", "doc/media", "user")
        self.assertNotIn("doc/media", self.ids(doc))
        self.assertIn("doc/media", self.ids(doc, exclude_linked=False))

    def test_series_members_collapse_into_one(self) -> None:
        doc = self.n.create(SCOPE, {"title": "冤种小徐｜就业与实习札记",
                                    "children": ["社会经验研究：绩点、实习的真实价值、就业竞争力。"]},
                            "agent:chatgpt")["doc_id"]
        s = suggest(self.conn, doc, SCOPE)
        series = [x for x in s if x["object_id"] in ("doc/s1", "doc/s2")]
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].get("similar_siblings"), 1)

    def test_plain_record_gets_nothing_far_fetched(self) -> None:
        doc = self.n.create(SCOPE, {"title": "2026-11-10｜早间健身反馈",
                                    "children": ["深蹲 5×5，状态一般。"]}, "agent:chatgpt")["doc_id"]
        self.assertEqual(self.ids(doc), [])

    def test_write_results_carry_suggestions_and_nothing_is_stored(self) -> None:
        server = build_server(self.db_path, SCOPE, agent_name="tester")
        before = self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        out = call(server, "nexus_create_document", ENTRY)
        self.assertEqual(out["status"], "OK")
        self.assertIn("doc/media", [s["object_id"] for s in out["suggested_links"]])
        self.assertIn("not relations", out["suggested_links_note"])
        edit = call(server, "nexus_edit_document", {
            "doc_id": out["doc_id"], "expected_revision": 1,
            "ops": [{"op": "insert", "parent_id": None, "after": None, "kind": "paragraph",
                     "body": "另见明日方舟卫戍协议阵容。"}]})
        self.assertEqual(edit["status"], "OK")
        self.assertIn("doc/game", [s["object_id"] for s in edit["suggested_links"]])
        after = sqlite_count(self.db_path)
        self.assertEqual(before, after)

    def test_related_lists_possibly_related_as_leads(self) -> None:
        doc = self.n.create(SCOPE, ENTRY, "agent:chatgpt")["doc_id"]
        self.conn.commit()
        tools = AgentTools(self.core, SCOPE)
        out = tools.nexus_related(doc)
        self.assertEqual(out["edges"], [])
        self.assertIn("doc/media", [m["object_id"] for m in out["possibly_related"]])
        self.assertIn("not relations", out["possibly_related_note"])
        self.assertNotIn("possibly_related", tools.nexus_related(doc, possibly_related=False))


def sqlite_count(path: str) -> int:
    import sqlite3
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    finally:
        c.close()
