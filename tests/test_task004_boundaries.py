"""Task 004 regression tests for Codex-discovered authority/correctness defects.

Every test asserts *content and database side effects*, not only a status code.
These tests are expected to fail against the Task 003 freeze.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from helpers import NexusTest
from nexus.db import canonical_json, sha256_text


class Task004Boundaries(NexusTest):
    # ------------------------------------------------------------- helpers
    def _set_ref_status(self, source_ref_id: str, status: str) -> None:
        self.store.conn.execute(
            "UPDATE source_refs SET status = ? WHERE source_ref_id = ?",
            (status, source_ref_id),
        )

    def _known_fact(self, fact: dict):
        fp = sha256_text(canonical_json(fact))
        return self.store.conn.execute(
            "SELECT * FROM known_facts WHERE fingerprint = ?", (fp,)
        ).fetchone()

    def _add_user_event(self, event_id: str, fact: dict, expected_revision,
                        authority: str = "user:owner") -> None:
        payload = {
            "fact": fact,
            "aspect": "decision",
            "changes": {"decision_id": fact["decision_id"]},
        }
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        self.store.add_event(
            event_id, "USER_DECISION", "2026-10-29T00:00:00Z",
            "project/DLC-01", payload, "src/dlc-mission-r1",
            authority, event_id,
        )

    def _decision(self, scope: str = "project:DLC-01"):
        return self.core.get_current_state(
            "project/DLC-01", aspect="decision", scope=scope)

    # ------------------------------------------------- 1. source access
    def test_blocked_aspect_hides_current_and_historical_payloads(self):
        self._set_ref_status("src/dlc-mission-r1", "BLOCKED")
        current = self._decision()
        self.assertStatus(current, "BLOCKED", "blocked current")
        self.assertIsNone(current["state"]["value"])
        self.assertNotIn("native_id", current["evidence"][0])
        self.assertNotIn("location", current["evidence"][0])

        self._set_ref_status("src/dlc-legacy-r1", "BLOCKED")
        historical = self.core.get_current_state(
            "discussion/DLC-01-LEGACY-2026-09", aspect="decision",
            scope="project:DLC-01")
        self.assertStatus(historical, "BLOCKED", "blocked historical")
        aspect = historical["result"]["aspects"][0]
        self.assertIsNone(aspect["value"])
        self.assertNotIn("historical_value", aspect)
        self.assertNotIn("NOT_DECISION", json.dumps(historical, ensure_ascii=False))
        self.record(
            "T4-S1", "BLOCKED 来源不泄露 Current 或 Historical 内容",
            {"blocked": ["src/dlc-mission-r1", "src/dlc-legacy-r1"]},
            {"resolution": "BLOCKED", "value": None, "historical_value": None},
            {"current": current["state"], "historical": aspect},
        )

    def test_missing_source_never_current(self):
        self._set_ref_status("src/dlc-mission-r1", "MISSING")
        env = self._decision()
        self.assertStatus(env, "MISSING", "missing source")
        self.assertFalse(env["state"]["is_latest_verified"])
        self.assertIsNone(env["state"]["value"])
        self.assertNotEqual(env["state"]["resolution"], "CURRENT")
        self.record(
            "T4-S2", "MISSING 来源不得返回最新 Current",
            {"source": "src/dlc-mission-r1", "status": "MISSING"},
            {"resolution": "MISSING", "value": None, "is_latest_verified": False},
            env["state"],
        )

    def test_blocked_observation_and_evidence_expansion_are_withheld(self):
        self.store.add_section(
            "src/video-a-001", "§protected", "protected",
            "PROTECTED-SECTION-BODY must never be returned")
        self._set_ref_status("src/video-a-001", "BLOCKED")
        env = self.core.get_context(
            "sponsorship", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        self.assertStatus(env, "BLOCKED", "blocked observation context")
        pkg = env["result"]["package"]
        self.assertEqual(pkg["observations"], [])
        self.assertNotIn("商单", json.dumps(env, ensure_ascii=False))
        self.assertNotIn("2023-12", json.dumps(env, ensure_ascii=False))
        self.assertNotIn("expanded_sections", pkg)
        self.assertNotIn("native_id", env["evidence"][0])
        self.assertTrue(any("OBSERVATIONS_WITHHELD" in w for w in env["warnings"]))

        trace = self.core.trace_evidence(
            "src/video-a-001", scope="creator:CREATOR-A", expand=True)
        self.assertStatus(trace, "BLOCKED", "blocked evidence expansion")
        self.assertEqual(trace["result"]["sections"], [])
        self.assertNotIn("PROTECTED-SECTION-BODY",
                         json.dumps(trace, ensure_ascii=False))
        self.record(
            "T4-S3", "BLOCKED Observation 与证据展开均不泄露正文",
            {"intent": "sponsorship", "expand": True},
            {"observations": [], "sections": [], "native_id_redacted": True},
            {"package": pkg, "trace_status": trace["execution"]["status"]},
        )

    def test_missing_observation_is_withheld(self):
        self._set_ref_status("src/video-a-001", "MISSING")
        env = self.core.get_context(
            "profit", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        self.assertStatus(env, "MISSING", "missing observation context")
        self.assertEqual(env["result"]["package"]["observations"], [])
        self.assertNotIn("4-6 万", json.dumps(env, ensure_ascii=False))

    def test_blocked_policy_is_not_used(self):
        self._set_ref_status("src/dlc-principle-r1", "BLOCKED")
        env = self.core.get_context(
            "start_class", "project/DLC-01", scope="project:DLC-01")
        pkg = env["result"]["package"]
        self.assertIsNone(pkg.get("policy"))
        self.assertNotIn("principle_object_id", json.dumps(pkg, ensure_ascii=False))
        self.record(
            "T4-S4", "BLOCKED 原则来源不得作为适用 Policy",
            {"intent": "start_class", "blocked": "src/dlc-principle-r1"},
            {"policy": None}, {"policy": pkg.get("policy")},
        )

    # --------------------------------------------- 2. relation verification
    def test_unverified_authored_relation_cannot_resolve_creator_profile(self):
        self.store.conn.execute(
            "UPDATE relations SET verification = 'UNVERIFIED' WHERE relation_id = 'rel-authored'")
        env = self.core.get_context(
            "sponsorship", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        profile = env["result"]["package"]["aspects"]["profile"]
        self.assertNotEqual(profile["resolution"], "CURRENT")
        self.assertIsNone(profile["value"])
        self.assertNotIn("2023-12", json.dumps(env, ensure_ascii=False))

        self.store.conn.execute(
            "UPDATE relations SET verification = 'VERIFIED' WHERE relation_id = 'rel-authored'")
        ok = self.core.get_context(
            "sponsorship", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        self.assertEqual(
            ok["result"]["package"]["aspects"]["profile"]["value"]["full_time_since"],
            "2023-12")
        self.record(
            "T4-R1", "UNVERIFIED AUTHORED 关系不得跨对象读取 Creator 身份",
            {"relation": "rel-authored"},
            {"unverified": "no profile value", "verified": "2023-12"},
            {"unverified": profile, "verified": "2023-12"},
        )

    def test_unverified_governed_by_is_candidate_only(self):
        self.store.conn.execute(
            "UPDATE relations SET verification = 'UNVERIFIED' WHERE relation_id = 'rel-governed'")
        env = self.core.get_context(
            "start_class", "project/DLC-01", scope="project:DLC-01")
        pkg = env["result"]["package"]
        self.assertIsNone(pkg.get("policy"))
        self.assertEqual(pkg["relations"], [])
        self.assertEqual(len(pkg["relation_candidates"]), 1)
        self.assertFalse(pkg["relation_candidates"][0]["effective"])
        self.assertTrue(any("UNVERIFIED_RELATIONS_EXCLUDED" in w for w in env["warnings"]))
        self.assertNotIn("principle_object_id", json.dumps(pkg, ensure_ascii=False))
        self.record(
            "T4-R2", "UNVERIFIED GOVERNED_BY 只作候选，不影响 Policy",
            {"relation": "rel-governed"},
            {"policy": None, "effective_relations": 0, "candidates": 1},
            {"policy": pkg.get("policy"),
             "relations": pkg["relations"],
             "candidates": pkg.get("relation_candidates")},
        )

    # --------------------------------- 3. event authority/revision/dedupe
    def test_unauthorized_event_does_not_poison_dedupe(self):
        fact = {"kind": "decision", "decision_id": "D4"}
        self._add_user_event("ev-user-d4", fact, expected_revision=1)
        denied = self.core.evaluate_event(
            "ev-wrong-owner", caller="system:test-harness", scope="project:DLC-01")
        self.assertStatus(denied, "AUTHORITY_DENIED", "wrong owner")
        self.assertEqual(self._decision()["state"]["value"]["decision_id"], "D1")
        self.assertIsNone(self._known_fact(fact))
        row = self.store.get_event("ev-wrong-owner")
        self.assertIsNone(row["processed_at"])

        applied = self.core.evaluate_event(
            "ev-user-d4", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(applied, "OK", "legal same fact")
        self.assertEqual(self._decision()["state"]["value"]["decision_id"], "D4")
        self.assertIsNotNone(self._known_fact(fact))
        self.record(
            "T4-E1", "越权事件被拒绝且不污染后续同事实合法事件",
            {"events": ["ev-wrong-owner", "ev-user-d4"]},
            {"denied": "AUTHORITY_DENIED", "then_applied": "D4"},
            {"denied": denied["error"], "final": "D4"},
        )

    def test_version_conflict_does_not_poison_dedupe(self):
        fact = {"kind": "decision", "decision_id": "D3"}
        self._add_user_event("ev-user-d3", fact, expected_revision=1)
        conflict = self.core.evaluate_event(
            "ev-user-stale", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(conflict, "VERSION_CONFLICT", "stale version")
        self.assertEqual(self._decision()["state"]["revision"], 1)
        self.assertIsNone(self._known_fact(fact))
        self.assertIsNone(self.store.get_event("ev-user-stale")["processed_at"])

        applied = self.core.evaluate_event(
            "ev-user-d3", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(applied, "OK", "legal same fact after conflict")
        self.assertEqual(self._decision()["state"]["value"]["decision_id"], "D3")
        self.record(
            "T4-E2", "VERSION_CONFLICT 不污染同事实的后续合法事件",
            {"events": ["ev-user-stale", "ev-user-d3"]},
            {"conflict": "VERSION_CONFLICT", "then_applied": "D3"},
            {"conflict": conflict["error"], "final": "D3"},
        )

    def test_expected_revision_is_required(self):
        fact = {"kind": "decision", "decision_id": "D5"}
        self._add_user_event("ev-user-no-expected", fact, expected_revision=None)
        env = self.core.evaluate_event(
            "ev-user-no-expected", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(env, "VERSION_CONFLICT", "missing expected revision")
        self.assertEqual(self._decision()["state"]["revision"], 1)
        self.assertIsNone(self._known_fact(fact))
        self.record(
            "T4-E3", "更新 Current 必须提供 expected_revision",
            {"event": "ev-user-no-expected"},
            {"status": "VERSION_CONFLICT", "revision_unchanged": 1},
            {"error": env["error"], "revision": 1},
        )

    def test_failed_apply_rolls_back_decision_dedupe_and_event(self):
        fact = {"kind": "decision", "decision_id": "D2"}
        with patch.object(self.store, "add_aspect", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.core.evaluate_event(
                    "ev-user-decision", caller="user:owner", scope="project:DLC-01")
        self.assertEqual(self._decision()["state"]["revision"], 1)
        self.assertEqual(self._decision()["state"]["value"]["decision_id"], "D1")
        self.assertIsNone(self._known_fact(fact))
        self.assertIsNone(self.store.get_event("ev-user-decision")["processed_at"])
        current_rows = self.store.get_aspects("project/DLC-01", current_only=True)
        self.assertEqual(len(current_rows), 3)  # decision/learner_state/status intact

        applied = self.core.evaluate_event(
            "ev-user-decision", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(applied, "OK", "retry after rollback")
        self.assertEqual(self._decision()["state"]["value"]["decision_id"], "D2")
        self.record(
            "T4-E4", "写入失败时事务回滚，不留半更新/去重记录",
            {"event": "ev-user-decision", "injected": "add_aspect raises"},
            {"after_failure": "D1/rev1/unprocessed", "after_retry": "D2"},
            {"after_failure": self._decision()["state"], "after_retry": "D2"},
        )

    def test_blocked_event_source_is_not_evaluated(self):
        self._set_ref_status("src/exu-email-r1", "BLOCKED")
        env = self.core.evaluate_event(
            "ev-official-new", caller="system:test-harness", scope="project:DLC-01")
        self.assertStatus(env, "BLOCKED", "blocked event source")
        self.assertIsNone(self._known_fact({"kind": "lab_schedule", "day": "Wednesday",
                                           "window": "16:00-20:00",
                                           "effective": "2026-11-15"}))
        self.assertIsNone(self.store.get_event("ev-official-new")["processed_at"])
        self.record(
            "T4-E5", "BLOCKED 来源事件不得被评估或写入去重",
            {"event": "ev-official-new", "source": "BLOCKED"},
            {"status": "BLOCKED", "dedupe": None, "processed": False},
            {"status": env["execution"]["status"]},
        )

    def test_event_scope_and_caller_validation(self):
        wrong_scope = self.core.evaluate_event(
            "ev-official-new", caller="system:test-harness", scope="course:DEMO0009")
        self.assertStatus(wrong_scope, "INVALID_SCOPE", "wrong event scope")
        unknown = self.core.evaluate_event(
            "ev-official-new", caller="client:attacker", scope="project:DLC-01")
        self.assertStatus(unknown, "AUTHORITY_DENIED", "unknown caller")
        missing = self.core.evaluate_event(
            "ev-official-new", caller=None, scope="project:DLC-01")
        self.assertStatus(missing, "AUTHORITY_DENIED", "missing caller")
        self.record(
            "T4-E6", "事件 scope 与调用者身份先于状态修改校验",
            {"callers": ["system:test-harness/course", "client:attacker", None]},
            {"INVALID_SCOPE", "AUTHORITY_DENIED", "AUTHORITY_DENIED"},
            {"scope": wrong_scope["error"]["code"],
             "caller": unknown["error"]["code"],
             "missing": missing["error"]["code"]},
        )


if __name__ == "__main__":
    unittest.main()
