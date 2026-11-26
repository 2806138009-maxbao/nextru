"""Task 004 round-3 counterexamples: scope, relation validity, revision parsing,
and status precedence."""

from __future__ import annotations

import json
import unittest

from helpers import NexusTest
from nexus.db import canonical_json, sha256_text


class Task004Round3(NexusTest):
    def _set_ref(self, ref_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.store.conn.execute(
            f"UPDATE source_refs SET {sets} WHERE source_ref_id = ?",
            (*fields.values(), ref_id),
        )

    def _add_ref(self, ref_id: str, scope: str, status: str = "OK") -> None:
        self.store.add_source_ref(
            ref_id, "SIM_VIDEO", f"sim-{ref_id}", "v1",
            f"sim://{ref_id}", "NO_TIMECODE", scope, status=status)

    def _add_obs(self, obs_id: str, ref_ids: list[str],
                 statement: str = "CROSS-SCOPE STATEMENT",
                 detail: str = "CROSS-SCOPE DETAIL") -> None:
        self.store.add_observation(
            obs_id, "video/CREATOR-A-V001", "commercial", statement, "CASE",
            detail, ref_ids)

    def _add_user_event(self, event_id: str, expected) -> None:
        payload = {
            "fact": {"kind": "decision", "decision_id": "DX"},
            "aspect": "decision",
            "changes": {"decision_id": "DX"},
        }
        if expected is not ...:
            payload["expected_revision"] = expected
        self.store.add_event(
            event_id, "USER_DECISION", "2026-10-29T00:00:00Z",
            "project/DLC-01", payload, "src/dlc-mission-r1",
            "user:owner", event_id)

    def _known(self, fact: dict):
        return self.store.conn.execute(
            "SELECT * FROM known_facts WHERE fingerprint = ?",
            (sha256_text(canonical_json(fact)),),
        ).fetchone()

    # ------------------------------------------------- 1. SourceRef scope
    def test_observation_source_ref_scope_is_enforced_everywhere(self):
        self._add_ref("src/cross-scope", "external:exu")
        self._add_obs("obs-cross-scope", ["src/cross-scope"])

        ctx = self.core.get_context(
            "profit", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        self.assertStatus(ctx, "INVALID_SCOPE", "cross-scope observation context")
        pkg = ctx["result"]["package"]
        self.assertIn("obs-cross-scope",
                      [o["observation_id"] for o in pkg["withheld_observations"]])
        self.assertNotIn("CROSS-SCOPE STATEMENT", json.dumps(ctx, ensure_ascii=False))

        trace_obs = self.core.trace_evidence(
            "obs-cross-scope", scope="creator:CREATOR-A")
        self.assertStatus(trace_obs, "INVALID_SCOPE", "cross-scope observation trace")
        self.assertNotIn("statement", trace_obs["result"]["item"]["observation"])
        self.assertNotIn("CROSS-SCOPE", json.dumps(trace_obs, ensure_ascii=False))

        direct_wrong = self.core.trace_evidence(
            "src/cross-scope", scope="creator:CREATOR-A", expand=True)
        self.assertStatus(direct_wrong, "INVALID_SCOPE", "direct cross-scope ref")
        self.assertIsNone(direct_wrong["result"])

        direct_ok = self.core.trace_evidence(
            "src/cross-scope", scope="external:exu", expand=True)
        self.assertStatus(direct_ok, "OK", "direct correct-scope ref")
        self.record(
            "T4R3-S1", "关联 SourceRef 的 access_scope 统一强制",
            {"ref": "src/cross-scope", "actual_scope": "external:exu",
             "requested": "creator:CREATOR-A"},
            {"context": "INVALID_SCOPE/withheld", "trace": "INVALID_SCOPE/no statement",
             "direct_wrong": "INVALID_SCOPE", "direct_ok": "OK"},
            {"context": ctx["execution"]["status"],
             "trace": trace_obs["execution"]["status"],
             "direct_wrong": direct_wrong["execution"]["status"],
             "direct_ok": direct_ok["execution"]["status"]},
        )

    def test_direct_global_source_read_cannot_bypass_scope(self):
        self._add_ref("src/global-bypass", "external:exu")
        self._add_obs("obs-global-bypass", ["src/global-bypass"])

        direct = self.core.trace_evidence(
            "src/global-bypass", scope="global", expand=True)
        self.assertStatus(direct, "INVALID_SCOPE", "direct global bypass")
        self.assertIsNone(direct["result"])

        indirect = self.core.trace_evidence(
            "obs-global-bypass", scope="global")
        self.assertStatus(indirect, "INVALID_SCOPE", "indirect global bypass")
        self.assertIsNone(indirect["result"])
        self.assertNotIn("CROSS-SCOPE", json.dumps(indirect, ensure_ascii=False))

        allowed = self.core.trace_evidence(
            "src/global-bypass", scope="external:exu")
        self.assertStatus(allowed, "OK", "explicit scope")
        self.record(
            "T4R3-S4", "global 直接读取不得绕过 SourceRef Scope",
            {"ref": "src/global-bypass", "ref_scope": "external:exu"},
            {"direct_global": "INVALID_SCOPE", "indirect_global": "INVALID_SCOPE",
             "explicit": "OK"},
            {"direct": direct["execution"]["status"],
             "indirect": indirect["execution"]["status"],
             "explicit": allowed["execution"]["status"]},
        )

    def test_aspect_version_source_ref_scope_is_enforced(self):
        self._add_ref("src/cross-aspect", "external:exu")
        self.store.add_aspect(
            "project/DLC-01:cross_note:r1", "project/DLC-01", "cross_note",
            {"text": "CROSS-ASPECT PAYLOAD"}, "1", "CURRENT", "VERIFIED", 1,
            True, None, None, "src/cross-aspect")
        env = self.core.get_current_state(
            "project/DLC-01", "cross_note", scope="project:DLC-01")
        self.assertStatus(env, "INVALID_SCOPE", "cross-scope aspect")
        self.assertIsNone(env["state"]["value"])
        self.assertNotIn("CROSS-ASPECT PAYLOAD", json.dumps(env, ensure_ascii=False))
        self.assertNotIn("native_id", env["evidence"][0])

        trace = self.core.trace_evidence(
            "project/DLC-01:cross_note:r1", scope="project:DLC-01")
        self.assertStatus(trace, "INVALID_SCOPE", "cross-scope aspect trace")
        self.assertNotIn("payload", trace["result"]["item"]["aspect"])
        self.record(
            "T4R3-S2", "Aspect 版本通过其他 Scope SourceRef 构造时被拒绝",
            {"aspect": "cross_note", "ref_scope": "external:exu"},
            {"status": "INVALID_SCOPE", "value": None, "payload": None},
            {"status": env["execution"]["status"]},
        )

    def test_relation_provenance_source_ref_scope_is_enforced(self):
        self._add_ref("src/cross-rel", "external:exu")
        self.store.conn.execute(
            "UPDATE relations SET provenance_refs = ? WHERE relation_id = 'rel-governed'",
            (json.dumps(["src/cross-rel"]),),
        )
        env = self.core.trace_evidence(
            "rel-governed", scope="project:DLC-01", expand=True)
        self.assertStatus(env, "INVALID_SCOPE", "cross-scope relation provenance")
        self.assertEqual(env["result"]["sections"], [])
        self.assertNotIn("external:exu", json.dumps(env["result"]["item"],
                                                    ensure_ascii=False))
        self.record(
            "T4R3-S3", "Relation provenance 的 SourceRef Scope 一致",
            {"relation": "rel-governed", "ref_scope": "external:exu"},
            {"status": "INVALID_SCOPE", "sections": []},
            {"status": env["execution"]["status"]},
        )

    # ------------------------------------------------- 2. Relation validity
    def test_future_valid_from_relation_is_not_effective(self):
        self.store.conn.execute(
            "UPDATE relations SET valid_from = '2027-01-01T00:00:00Z'"
            " WHERE relation_id = 'rel-authored'")
        ctx = self.core.get_context(
            "sponsorship", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        self.assertNotEqual(
            ctx["result"]["package"]["aspects"]["profile"]["resolution"], "CURRENT")
        self.assertIsNone(ctx["result"]["package"]["aspects"]["profile"]["value"])

        default = self.core.traverse_relations(
            "creator/CREATOR-A", relation_types=["AUTHORED"],
            direction="out", max_depth=1, scope="creator:CREATOR-A")
        self.assertEqual(default["result"]["edges"], [])
        self.assertEqual(default["result"]["excluded_candidates"][0]["excluded_reason"],
                         "INACTIVE_RELATION")

        candidates = self.core.traverse_relations(
            "creator/CREATOR-A", relation_types=["AUTHORED"],
            direction="out", max_depth=1, scope="creator:CREATOR-A",
            include_candidates=True)
        edge = candidates["result"]["edges"][0]
        self.assertFalse(edge["effective"])
        self.assertTrue(edge["inactive"])
        self.record(
            "T4R3-R1", "valid_from 未到的关系不得作为有效关系",
            {"relation": "rel-authored", "valid_from": "2027-01-01"},
            {"profile": "not resolved", "default": "excluded",
             "candidate": {"effective": False, "inactive": True}},
            {"profile": ctx["result"]["package"]["aspects"]["profile"],
             "edge": edge},
        )

    def test_past_valid_to_relation_is_not_effective(self):
        self.store.conn.execute(
            "UPDATE relations SET valid_to = '2026-01-01T00:00:00Z'"
            " WHERE relation_id = 'rel-governed'")
        env = self.core.get_context(
            "start_class", "project/DLC-01", scope="project:DLC-01")
        pkg = env["result"]["package"]
        self.assertIsNone(pkg["policy"])
        self.assertEqual(pkg["relations"], [])
        candidate = pkg["relation_candidates"][0]
        self.assertFalse(candidate["effective"])
        self.assertTrue(candidate["inactive"])
        self.record(
            "T4R3-R2", "valid_to 已过的关系不得作为有效 Policy 依据",
            {"relation": "rel-governed", "valid_to": "2026-01-01"},
            {"policy": None, "effective_relations": 0,
             "candidate": {"effective": False, "inactive": True}},
            {"policy": pkg["policy"], "candidate": candidate},
        )

    # --------------------------------------------- 3. Revision input
    def test_invalid_expected_revision_values_are_rejected_consistently(self):
        cases = [1.5, "1.5", "abc", True, -1, None]
        results = []
        for i, value in enumerate(cases):
            event_id = f"ev-invalid-rev-{i}"
            if value is None:
                self._add_user_event(event_id, ...)
            else:
                self._add_user_event(event_id, value)
            env = self.core.evaluate_event(
                event_id, caller="user:owner", scope="project:DLC-01")
            self.assertStatus(env, "VERSION_CONFLICT", f"invalid revision {value!r}")
            self.assertEqual(self.core.get_current_state(
                "project/DLC-01", "decision", "project:DLC-01"
            )["state"]["revision"], 1)
            self.assertIsNone(self._known({"kind": "decision", "decision_id": "DX"}))
            self.assertIsNone(self.store.get_event(event_id)["processed_at"])
            results.append({"value": repr(value), "status": env["execution"]["status"]})
        self.record(
            "T4R3-V1", "expected_revision 仅接受非负整数，非法值无副作用",
            {"values": [repr(v) for v in cases]},
            {"all": "VERSION_CONFLICT", "revision": 1, "dedupe": None,
             "processed": False},
            {"results": results},
        )

    # ------------------------------------------------- 4. Status precedence
    def test_budget_does_not_rewrite_blocked_to_partial(self):
        self.store.conn.execute(
            "UPDATE source_refs SET status = 'BLOCKED'"
            " WHERE source_ref_id = 'src/video-a-001'")
        env = self.core.get_context(
            "sponsorship", "video/CREATOR-A-V001", scope="creator:CREATOR-A",
            max_chars=100)
        self.assertStatus(env, "BLOCKED", "blocked + budget")
        pkg = env["result"]["package"]
        self.assertTrue(pkg["budget"]["exceeded"])
        self.assertTrue(any("CONTEXT_BUDGET_EXCEEDED" in w for w in env["warnings"]))
        self.assertNotIn("2023-12", json.dumps(env, ensure_ascii=False))
        self.record(
            "T4R3-P1", "预算不足不得把 BLOCKED 改写为 PARTIAL",
            {"source": "BLOCKED", "max_chars": 100},
            {"status": "BLOCKED", "budget_exceeded": True},
            {"status": env["execution"]["status"], "budget": pkg["budget"]},
        )

    def test_budget_does_not_rewrite_missing_to_partial(self):
        self.store.conn.execute(
            "UPDATE source_refs SET status = 'MISSING'"
            " WHERE source_ref_id = 'src/video-a-001'")
        env = self.core.get_context(
            "sponsorship", "video/CREATOR-A-V001", scope="creator:CREATOR-A",
            max_chars=100)
        self.assertStatus(env, "MISSING", "missing + budget")
        pkg = env["result"]["package"]
        self.assertTrue(pkg["budget"]["exceeded"])
        self.assertTrue(any("CONTEXT_BUDGET_EXCEEDED" in w for w in env["warnings"]))
        self.record(
            "T4R3-P2", "预算不足不得把 MISSING 改写为 PARTIAL",
            {"source": "MISSING", "max_chars": 100},
            {"status": "MISSING", "budget_exceeded": True},
            {"status": env["execution"]["status"], "budget": pkg["budget"]},
        )

    def test_budget_alone_is_partial_with_diagnostic(self):
        env = self.core.get_context(
            "first_video_status", "creative/FIRST-VIDEO",
            scope="creative:first-video", max_chars=100)
        self.assertStatus(env, "PARTIAL", "budget only")
        self.assertTrue(env["result"]["package"]["budget"]["exceeded"])
        self.assertTrue(any("CONTEXT_BUDGET_EXCEEDED" in w for w in env["warnings"]))


if __name__ == "__main__":
    unittest.main()
