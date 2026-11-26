"""nexus_read on long documents: never report a cut-off read as complete.

The core's research reading trims segments to a 40k-char JSON budget. nexus_read pages on its own,
so it must return every paragraph across pages, and say `complete: false` until the last page.
Synthetic documents only.
"""

from __future__ import annotations

from helpers import NexusTest

from nexus.agent_tools import AgentTools
from nexus.ingest import FetchedDocument, ingest_document

SCOPE = "research:paging"
N = 400


def long_doc() -> FetchedDocument:
    lines = ["LONG｜Journal"] + [f"Entry {i:03d}｜" + "伦敦生活记录。" * 20 for i in range(1, N)]
    text = "\n".join(lines) + "\n"
    return FetchedDocument("GOOGLE_DRIVE", "SIMLONG", "LONG｜Journal", "5", "2026-11-04T18:28:06Z", None, text)


class ReadPagingTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        ingest_document(self.store, long_doc(), SCOPE, object_id="doc/long")
        self.tools = AgentTools(self.core, SCOPE)

    def test_paging_reaches_every_paragraph(self) -> None:
        first = self.tools.nexus_read("doc/long", max_chars=12000)
        self.assertEqual(first["paragraphs_total"], N)
        self.assertEqual(first["status"], "PARTIAL")
        self.assertFalse(first["complete"])
        seen, off, last = [], 0, None
        while True:
            last = self.tools.nexus_read("doc/long", offset=off, max_chars=12000)
            seen += [p["anchor"] for p in last["paragraphs"]]
            if "next_offset" not in last:
                break
            off = last["next_offset"]
        self.assertEqual(len(seen), N)
        self.assertEqual(len(set(seen)), N)
        self.assertTrue(last["complete"])
        self.assertTrue(seen[-1].startswith(f"h0.{N}-"))

    def test_late_anchor_and_bare_position(self) -> None:
        full = self.tools.nexus_read("doc/long", anchors=[f"h0.{N - 1}"])
        self.assertEqual(len(full["paragraphs"]), 1)
        anchor = full["paragraphs"][0]["anchor"]
        self.assertTrue(anchor.startswith(f"h0.{N - 1}-"))
        again = self.tools.nexus_read("doc/long", anchors=[anchor, "h0.5-deadbeef"])
        self.assertEqual([p["anchor"] for p in again["paragraphs"]], [anchor])
        self.assertEqual(again["anchors_not_found"], ["h0.5-deadbeef"])
        self.assertIn("anchors_hint", again)

    def test_character_offset_gets_a_clear_note(self) -> None:
        out = self.tools.nexus_read("doc/long", offset=16000)
        self.assertFalse(out["complete"])
        self.assertIn("offset counts paragraphs", out["note"])
        self.assertNotIn("returned", out)


class BudgetTrimTests(NexusTest):
    """The core's budget trim (binary search) drops exactly what one-at-a-time popping dropped."""

    def test_trim_matches_one_at_a_time(self) -> None:
        import copy
        from nexus import research
        ingest_document(self.store, long_doc(), SCOPE, object_id="doc/long")
        for budget in (3000, 12000, 40000):
            fast = research.build_research_evidence(self.core, "doc/long", SCOPE, max_chars=budget)["result"]
            slow = research.build_research_evidence(self.core, "doc/long", SCOPE, max_chars=10 ** 9)["result"]
            pack = copy.deepcopy(slow)
            pack["budget"]["max_chars"] = budget
            pack["budget"]["dropped_segments"] = []
            research._finalize_chars(pack)
            while pack["budget"]["chars"] > budget:
                reading = next((r for r in reversed(pack["readings"]) if r.get("segments")), None)
                if reading is None:
                    break
                pack["budget"]["dropped_segments"].append(reading["segments"].pop()["id"])
                reading["coverage"]["segments_returned"] = len(reading["segments"])
                reading["coverage"]["complete"] = False
                research._finalize_chars(pack)
            self.assertEqual(fast["budget"]["dropped_segments"], pack["budget"]["dropped_segments"])
            self.assertEqual([s["id"] for s in fast["readings"][0]["segments"]],
                             [s["id"] for s in pack["readings"][0]["segments"]])
            if fast["budget"]["exceeded"]:     # envelope alone is larger than a tiny budget
                self.assertEqual(fast["readings"][0]["segments"], [])
            else:
                self.assertLessEqual(fast["budget"]["chars"], budget)
