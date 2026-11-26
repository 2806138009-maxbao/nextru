"""Relatedness spectrum (nexus.relatedness): every related object once, in its strongest tier.

The case behind it (2026-11-12): a chest-training record listed 提瓦特战力评估 and Nexus 第三阶段思考
as "possibly related" (title bigrams 与当 / 当前 / 反馈 / 阶段), while two records of the same
training collection were not tied to it at all.
"""

from __future__ import annotations

from helpers import NexusTest

from nexus.agent_tools import AgentTools
from nexus.native import NativeDocs
from nexus.relatedness import shared_words, spectrum, strong_enough

SCOPE = "research:spectrum"
CHEST = {"title": "2026-11-12｜胸部训练反馈与当日结算",
         "children": ["哑铃卧推 7.5kg × 12；与当前阶段的反馈一致，真实世界里先控制动作。",
                      "下次参考健身动作技巧候选里的夹胸意象。"]}


class SpectrumTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        st = self.store
        with st.tx():
            st.add_scope(SCOPE, "research", "spectrum test")
            for oid, typ, name in (
                    ("ent/training_log/t", "training_log", "训练记录｜健身与力量训练"),
                    ("doc/morning", "document", "2026-11-10｜早间健布反馈（进行中）"),
                    ("doc/tips", "document", "健身动作技巧候选｜七条视频原文"),
                    ("doc/teyvat", "document", "06｜剧情与世界观｜提瓦特战力评估框架与当前排行"),
                    ("doc/phase3", "document", "Nexus｜第三阶段思考：功能块 × 真实世界反馈"),
                    ("doc/drive", "document", "00｜START HERE｜Google Drive 总入口"),
                    ("ent/idea/m", "idea", "母题学习法"),
                    ("doc/receipt", "document", "RECEIPT 012｜PS2 Ex4 课堂"),
                    ("doc/plan", "document", "阶段计划｜下一步"),
                    ("doc/decision", "document", "决定｜换课"),
                    ("ent/collection/big", "collection", "收件箱")):
                st.add_object(oid, typ, name, SCOPE)
            for i in range(45):
                st.add_object(f"doc/inbox{i}", "document", f"收件 {i} 号杂项", SCOPE)
                st.add_relation(f"rel/in{i}", f"doc/inbox{i}", "PART_OF", "ent/collection/big",
                                "VERIFIED", SCOPE, [])
            for i in range(30):
                st.add_object(f"doc/filler{i}", "document", f"填充 {i} 号｜Google 项目 Drive", SCOPE)
            st.add_relation("rel/m1", "doc/morning", "PART_OF", "ent/training_log/t", "VERIFIED", SCOPE, [])
            st.add_relation("rel/m2", "doc/tips", "PART_OF", "ent/training_log/t", "VERIFIED", SCOPE, [])
            st.add_relation("rel/x1", "doc/receipt", "EXEMPLIFIES", "ent/idea/m", "VERIFIED", SCOPE, [])
        self.conn.commit()
        self.n = NativeDocs(self.conn, agent_writes=True)
        self.chest = self.n.create(SCOPE, CHEST, "user")["doc_id"]
        self.n.add_relation(SCOPE, self.chest, "PART_OF", "ent/training_log/t", "user")
        self.n.add_relation(SCOPE, self.chest, "PART_OF", "ent/collection/big", "user")
        with self.store.tx():
            self.store.add_relation("rel/c1", self.chest, "BASED_ON", "doc/plan", "UNVERIFIED", SCOPE, [])
            self.store.add_relation("rel/c2", self.chest, "HAS_DECISION", "doc/decision", "UNVERIFIED", SCOPE, [])
            self.store.add_relation("rel/a1", self.chest, "EXEMPLIFIES", "ent/idea/m", "VERIFIED", SCOPE, [])
            self.store.add_relation("rel/a2", self.chest, "BASED_ON", "ent/idea/m", "UNVERIFIED", SCOPE, [])
        self.conn.commit()

    def sp(self, oid=None) -> dict:
        return spectrum(self.conn, oid or self.chest, SCOPE)

    def tiers(self, oid=None) -> dict[str, str]:
        return {i["object_id"]: i["tier"] for i in self.sp(oid)["items"]}

    def test_relations_land_in_their_tiers(self) -> None:
        t = self.tiers()
        self.assertEqual(t["ent/idea/m"], "A")
        self.assertEqual(t["ent/training_log/t"], "B")
        self.assertEqual(t["doc/plan"], "C")
        self.assertEqual(t["doc/decision"], "C")
        items = {i["object_id"]: i for i in self.sp()["items"]}
        self.assertTrue(items["doc/decision"].get("gate"))
        self.assertNotIn("gate", items["doc/plan"])

    def test_each_object_once_in_its_strongest_tier(self) -> None:
        items = [i for i in self.sp()["items"] if i["object_id"] == "ent/idea/m"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tier"], "A")
        self.assertEqual({r["type"] for r in items[0]["basis"]["relations"]}, {"EXEMPLIFIES", "BASED_ON"})

    def test_order_is_tier_then_gate(self) -> None:
        order = [i["tier"] + ("1" if i.get("gate") else "2") for i in self.sp()["items"]]
        self.assertEqual(order, sorted(order, key=lambda x: ("ABCDE".index(x[0]), x[1])))
        ids = [i["object_id"] for i in self.sp()["items"]]
        self.assertLess(ids.index("doc/decision"), ids.index("doc/plan"))

    def test_siblings_in_a_small_collection_are_structurally_close(self) -> None:
        t = self.tiers()
        self.assertEqual(t.get("doc/morning"), "D")
        self.assertEqual(t.get("doc/tips"), "D")          # D beats E: it also names 健身动作技巧候选
        d = next(i for i in self.sp()["items"] if i["object_id"] == "doc/morning")
        self.assertEqual(d["basis"]["shared_neighbours"][0]["object_id"], "ent/training_log/t")

    def test_a_large_collection_does_not_make_siblings_close(self) -> None:
        self.assertFalse(any(k.startswith("doc/inbox") for k in self.tiers()))

    def test_generic_and_straddling_words_are_not_text_matches(self) -> None:
        t = self.tiers()
        self.assertNotIn("doc/teyvat", t)                  # 与当 / 当前 / 评估
        self.assertNotIn("doc/phase3", t)                  # 阶段 / 真实世界反馈

    def test_text_tier_needs_real_words(self) -> None:
        doc = self.n.create(SCOPE, {"title": "随手记", "children": [
            "今天把健身动作技巧候选的第三条试了一下。", "Google Drive 的东西都搬走了。"]}, "user")["doc_id"]
        items = {i["object_id"]: i for i in self.sp(doc)["items"]}
        self.assertEqual(items["doc/tips"]["tier"], "E")
        self.assertIn("健身动作技巧候选", items["doc/tips"]["basis"]["words"])
        self.assertNotIn("doc/drive", items)               # two common English words only

    def test_shared_words_merge_and_weigh(self) -> None:
        self.assertEqual([w for w, _ in shared_words("健身动作技巧候选", {"健身", "身动", "动作", "作技"})],
                         ["健身动作技"])
        self.assertEqual(shared_words("真实世界反馈", {"真实", "实世", "世界", "界反", "反馈"}), [])
        self.assertEqual(shared_words("提瓦特战力评估框架与当前排行", {"与当", "当前", "评估"}), [])
        self.assertTrue(strong_enough(shared_words("母题学习法", {"母题", "题学", "学习", "习法"})))
        self.assertFalse(strong_enough([("Google", 2), ("Drive", 2)]))

    def test_archived_documents_are_left_out(self) -> None:
        doc = self.n.create(SCOPE, {"title": "早间健身补记", "children": ["x"]}, "user")["doc_id"]
        self.n.add_relation(SCOPE, doc, "PART_OF", "ent/training_log/t", "user")
        self.assertIn(doc, self.tiers())
        self.n.archive(doc)
        self.assertNotIn(doc, self.tiers())

    def test_nexus_related_keeps_old_fields_and_adds_spectrum(self) -> None:
        tools = AgentTools(self.core, SCOPE)
        old = tools.nexus_related(self.chest, spectrum=False)
        new = tools.nexus_related(self.chest)
        self.assertEqual({k: v for k, v in new.items() if not k.startswith("spectrum")}, old)
        first = new["spectrum"][0]
        self.assertEqual((first["object_id"], first["tier"], first["via"]), ("ent/idea/m", "A", ["BASED_ON", "EXEMPLIFIES"]))
        self.assertEqual(new["spectrum_counts"]["A"], 1)
        self.assertIn("spectrum_note", new)
