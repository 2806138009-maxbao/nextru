"""Task 004 second-round counterexamples (Codex REQUEST_CHANGES).

These target unified evidence access, multi-source decisions, cached-event
reauthorization, write policy, context withholding and historical Authority.
"""

from __future__ import annotations

import json
import unittest

from helpers import NexusTest
from nexus.db import canonical_json, sha256_text


class Task004Round2(NexusTest):
    def _set_ref(self, source_ref_id: str, status: str) -> None:
        self.store.conn.execute(
            "UPDATE source_refs SET status = ? WHERE source_ref_id = ?",
            (status, source_ref_id),
        )

    def _add_ref(self, ref_id: str, status: str,
                 scope: str = "creator:CREATOR-A") -> None:
        self.store.add_source_ref(
            ref_id, "SIM_VIDEO", f"sim-{ref_id}", "v1",
            f"sim://{ref_id}", "NO_TIMECODE", scope, status=status)

    def _add_obs(self, obs_id: str, ref_ids: list[str],
                 statement: str = "PROTECTED STATEMENT",
                 detail: str = "PROTECTED DETAIL") -> None:
        self.store.add_observation(
            obs_id, "video/CREATOR-A-V001", "commercial", statement, "CASE",
            detail, ref_ids)

    def _known(self, fact: dict):
        return self.store.conn.execute(
            "SELECT * FROM known_facts WHERE fingerprint = ?",
            (sha256_text(canonical_json(fact)),),
        ).fetchone()

    # ------------------------------------------------ trace_evidence access
    def test_trace_observation_scope_mismatch_rejected(self):
        env = self.core.trace_evidence(
            "obs-sponsor-1", scope="project:DLC-01")
        self.assertStatus(env, "INVALID_SCOPE", "trace scope mismatch")
        self.assertNotIn("商单", json.dumps(env, ensure_ascii=False))
        self.record(
            "T4R2-T1", "trace_evidence 校验目标对象 Scope",
            {"item": "obs-sponsor-1", "scope": "project:DLC-01"},
            {"status": "INVALID_SCOPE", "content": None},
            {"status": env["execution"]["status"]},
        )

    def test_trace_blocked_observation_hides_statement_detail(self):
        self._set_ref("src/video-a-001", "BLOCKED")
        env = self.core.trace_evidence(
            "obs-sponsor-1", scope="creator:CREATOR-A", expand=True)
        self.assertStatus(env, "BLOCKED", "blocked observation trace")
        item = env["result"]["item"]["observation"]
        self.assertNotIn("statement", item)
        self.assertNotIn("detail", item)
        self.assertNotIn("商单", json.dumps(env, ensure_ascii=False))
        self.assertEqual(env["result"]["sections"], [])
        self.assertIn("src/video-a-001", env["result"]["inaccessible_source_refs"])
        self.record(
            "T4R2-T2", "BLOCKED Observation 不返回 statement/detail/正文",
            {"item": "obs-sponsor-1", "expand": True},
            {"status": "BLOCKED", "statement": None, "sections": []},
            {"item": item, "inaccessible": env["result"]["inaccessible_source_refs"]},
        )

    def test_trace_multisource_observation_is_partial_not_complete(self):
        self._add_ref("src/extra-ok", "OK")
        self._add_ref("src/extra-blocked", "BLOCKED")
        self._add_obs("obs-multi", ["src/extra-ok", "src/extra-blocked"])
        env = self.core.trace_evidence(
            "obs-multi", scope="creator:CREATOR-A")
        self.assertStatus(env, "PARTIAL", "mixed source access")
        item = env["result"]["item"]["observation"]
        self.assertNotIn("statement", item)
        self.assertNotIn("detail", item)
        self.assertNotIn("PROTECTED", json.dumps(env, ensure_ascii=False))
        self.assertEqual(set(env["result"]["inaccessible_source_refs"]),
                         {"src/extra-blocked"})
        self.record(
            "T4R2-T3", "多来源任一不可访问即不得宣称完整核验",
            {"refs": ["src/extra-ok", "src/extra-blocked"]},
            {"status": "PARTIAL", "statement": None,
             "inaccessible": ["src/extra-blocked"]},
            {"status": env["execution"]["status"], "item": item},
        )

    def test_trace_blocked_relation_returns_no_raw_row_or_sections(self):
        self._set_ref("src/dlc-principle-r1", "BLOCKED")
        env = self.core.trace_evidence(
            "rel-governed", scope="project:DLC-01", expand=True)
        self.assertStatus(env, "BLOCKED", "blocked relation provenance")
        rel = env["result"]["item"]["relation"]
        self.assertNotIn("created_at", rel)
        self.assertNotIn("payload", rel)
        self.assertEqual(env["result"]["sections"], [])
        self.assertNotIn("最小闭环", json.dumps(env, ensure_ascii=False))
        self.record(
            "T4R2-T4", "BLOCKED Relation 不返回原始行/正文",
            {"item": "rel-governed", "expand": True},
            {"status": "BLOCKED", "raw_payload": None, "sections": []},
            {"status": env["execution"]["status"], "relation": rel},
        )

    def test_trace_blocked_aspect_version_has_no_payload(self):
        self._set_ref("src/dlc-mission-r1", "BLOCKED")
        env = self.core.trace_evidence(
            "project/DLC-01:decision:r1", scope="project:DLC-01")
        self.assertStatus(env, "BLOCKED", "blocked aspect evidence")
        aspect = env["result"]["item"]["aspect"]
        self.assertNotIn("payload", aspect)
        self.assertNotIn("模块 A", json.dumps(env, ensure_ascii=False))
        self.record(
            "T4R2-T5", "BLOCKED Aspect 版本不返回 payload",
            {"item": "project/DLC-01:decision:r1"},
            {"status": "BLOCKED", "payload": None},
            {"status": env["execution"]["status"], "aspect": aspect},
        )

    def test_trace_source_ref_scope_and_blocked_sections(self):
        wrong = self.core.trace_evidence(
            "src/video-a-001", scope="project:DLC-01", expand=True)
        self.assertStatus(wrong, "INVALID_SCOPE", "source ref scope")

        self.store.add_section("src/video-a-001", "§p", "p",
                               "PROTECTED-SECTION-BODY")
        self._set_ref("src/video-a-001", "BLOCKED")
        blocked = self.core.trace_evidence(
            "src/video-a-001", scope="creator:CREATOR-A", expand=True)
        self.assertStatus(blocked, "BLOCKED", "source ref blocked")
        self.assertEqual(blocked["result"]["sections"], [])
        self.assertNotIn("PROTECTED-SECTION-BODY",
                         json.dumps(blocked, ensure_ascii=False))
        self.record(
            "T4R2-T6", "SourceRef Scope/访问状态统一校验，正文不泄露",
            {"scope_mismatch": True, "blocked": True},
            {"wrong": "INVALID_SCOPE", "blocked": "BLOCKED", "sections": []},
            {"wrong": wrong["execution"]["status"],
             "blocked": blocked["execution"]["status"]},
        )

    def test_same_blocked_resource_across_context_and_trace(self):
        self._add_ref("src/blocked-obs", "BLOCKED")
        self._add_obs("obs-blocked", ["src/blocked-obs"])
        ctx = self.core.get_context(
            "profit", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        self.assertStatus(ctx, "PARTIAL", "context partial observation")
        pkg = ctx["result"]["package"]
        self.assertNotIn("obs-blocked",
                         [o["observation_id"] for o in pkg["observations"]])
        self.assertIn("obs-blocked",
                      [o["observation_id"] for o in pkg["withheld_observations"]])
        self.assertNotIn("PROTECTED STATEMENT", json.dumps(ctx, ensure_ascii=False))

        trace = self.core.trace_evidence(
            "obs-blocked", scope="creator:CREATOR-A")
        self.assertStatus(trace, "BLOCKED", "trace same blocked obs")
        self.assertNotIn("statement", trace["result"]["item"]["observation"])
        self.assertNotIn("PROTECTED STATEMENT",
                         json.dumps(trace, ensure_ascii=False))
        self.record(
            "T4R2-T7", "同一资源经 Context 与 trace_evidence 均被拒绝",
            {"observation": "obs-blocked"},
            {"context": "PARTIAL/withheld", "trace": "BLOCKED/no statement"},
            {"context": ctx["execution"]["status"],
             "trace": trace["execution"]["status"]},
        )

    # ------------------------------------------- cached event reauthorization
    def test_cached_event_reauthorized_and_payload_hidden(self):
        applied = self.core.evaluate_event(
            "ev-official-new", caller="system:test-harness",
            scope="project:DLC-01")
        self.assertStatus(applied, "OK", "initial event")
        self._set_ref("src/exu-email-r1", "BLOCKED")

        blocked = self.core.evaluate_event(
            "ev-official-new", caller="system:test-harness",
            scope="project:DLC-01")
        self.assertStatus(blocked, "BLOCKED", "cached event after revoke")
        self.assertNotIn("outcome", blocked["result"] or {})
        self.assertNotIn("payload", json.dumps(blocked, ensure_ascii=False))
        self.assertTrue(self.store.get_event("ev-official-new")["processed_at"])

        self._set_ref("src/exu-email-r1", "OK")
        cached = self.core.evaluate_event(
            "ev-official-new", caller="system:test-harness",
            scope="project:DLC-01")
        self.assertStatus(cached, "OK", "cached event after restore")
        self.assertTrue(cached["result"]["idempotent"])
        self.assertNotIn("payload", cached["result"]["event"])
        self.assertNotIn("payload", json.dumps(cached, ensure_ascii=False))
        self.record(
            "T4R2-E1", "缓存事件先重新授权，撤销权限后不泄露 Outcome/Payload",
            {"event": "ev-official-new", "revoke_then_restore": True},
            {"revoked": "BLOCKED/no outcome", "restored": "OK/cached/no payload"},
            {"revoked": blocked["execution"]["status"],
             "restored": cached["execution"]["status"]},
        )

    def test_cached_event_rechecks_caller_and_scope(self):
        self.core.evaluate_event(
            "ev-official-new", caller="system:test-harness",
            scope="project:DLC-01")
        wrong_caller = self.core.evaluate_event(
            "ev-official-new", caller="client:attacker",
            scope="project:DLC-01")
        self.assertStatus(wrong_caller, "AUTHORITY_DENIED", "cached caller")
        wrong_scope = self.core.evaluate_event(
            "ev-official-new", caller="system:test-harness",
            scope="course:DEMO0009")
        self.assertStatus(wrong_scope, "INVALID_SCOPE", "cached scope")
        self.assertNotIn("outcome", wrong_caller["result"] or {})
        self.assertNotIn("outcome", wrong_scope["result"] or {})
        self.record(
            "T4R2-E2", "缓存事件重新校验 Caller 与 Scope",
            {"caller": "client:attacker", "scope": "course:DEMO0009"},
            {"caller": "AUTHORITY_DENIED", "scope": "INVALID_SCOPE"},
            {"caller": wrong_caller["execution"]["status"],
             "scope": wrong_scope["execution"]["status"]},
        )

    # ------------------------------------------------------ write policy
    def test_read_only_binding_rejects_user_decision_write(self):
        self.store.conn.execute(
            "UPDATE authority_bindings SET write_policy = 'READ_ONLY'"
            " WHERE binding_id = 'auth-decision'")
        env = self.core.evaluate_event(
            "ev-user-decision", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(env, "AUTHORITY_DENIED", "read-only binding")
        self.assertEqual(self.core.get_current_state(
            "project/DLC-01", "decision", "project:DLC-01"
        )["state"]["revision"], 1)
        self.assertIsNone(self._known({"kind": "decision", "decision_id": "D2"}))
        self.assertIsNone(self.store.get_event("ev-user-decision")["processed_at"])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM aspects WHERE object_id='project/DLC-01'"
                " AND is_current=1").fetchone()[0], 3)

        self.store.conn.execute(
            "UPDATE authority_bindings SET write_policy = 'USER_ONLY'"
            " WHERE binding_id = 'auth-decision'")
        ok = self.core.evaluate_event(
            "ev-user-decision", caller="user:owner", scope="project:DLC-01")
        self.assertStatus(ok, "OK", "restored binding")
        self.assertEqual(self.core.get_current_state(
            "project/DLC-01", "decision", "project:DLC-01"
        )["state"]["value"]["decision_id"], "D2")
        self.record(
            "T4R2-W1", "READ_ONLY AuthorityBinding 拒绝用户决定写入",
            {"binding": "auth-decision", "write_policy": "READ_ONLY"},
            {"status": "AUTHORITY_DENIED", "revision": 1, "dedupe": None,
             "processed": False},
            {"status": env["execution"]["status"], "after_restore": "D2"},
        )

    # --------------------------------------------------- context status
    def test_context_withheld_observation_is_partial(self):
        self._add_ref("src/blocked-obs2", "BLOCKED")
        self._add_obs("obs-blocked2", ["src/blocked-obs2"])
        env = self.core.get_context(
            "profit", "video/CREATOR-A-V001", scope="creator:CREATOR-A")
        self.assertStatus(env, "PARTIAL", "withheld observation")
        pkg = env["result"]["package"]
        self.assertIn("obs-blocked2",
                      [o["observation_id"] for o in pkg["withheld_observations"]])
        self.assertTrue(any("OBSERVATIONS_WITHHELD" in w for w in env["warnings"]))
        self.record(
            "T4R2-C1", "Observation 被扣除时 Context 不得完整成功",
            {"intent": "profit", "withheld": "obs-blocked2"},
            {"status": "PARTIAL", "withheld_present": True},
            {"status": env["execution"]["status"],
             "withheld": pkg["withheld_observations"]},
        )

    def test_context_blocked_policy_is_blocked_with_reason(self):
        self._set_ref("src/dlc-principle-r1", "BLOCKED")
        env = self.core.get_context(
            "start_class", "project/DLC-01", scope="project:DLC-01")
        self.assertStatus(env, "BLOCKED", "blocked policy")
        pkg = env["result"]["package"]
        self.assertIsNone(pkg["policy"])
        self.assertEqual(pkg["policy_status"], "BLOCKED")
        self.assertTrue(any("POLICY_WITHHELD" in w for w in env["warnings"]))

        self._set_ref("src/dlc-principle-r1", "MISSING")
        missing = self.core.get_context(
            "start_class", "project/DLC-01", scope="project:DLC-01")
        self.assertStatus(missing, "MISSING", "missing policy")
        self.assertEqual(missing["result"]["package"]["policy_status"], "MISSING")
        self.record(
            "T4R2-C2", "Policy 被扣除时 Context 状态与原因正确",
            {"policy_source": "src/dlc-principle-r1"},
            {"blocked": "BLOCKED", "missing": "MISSING",
             "policy_status": ["BLOCKED", "MISSING"]},
            {"blocked": env["execution"]["status"],
             "missing": missing["execution"]["status"]},
        )

    # ------------------------------------------------- historical Authority
    def test_as_of_before_binding_is_unverified(self):
        before = self.core.get_current_state(
            "project/DLC-01", "decision", "project:DLC-01",
            as_of="2026-10-15T00:00:00Z")
        self.assertStatus(before, "UNKNOWN", "as_of before binding")
        self.assertEqual(before["state"]["resolution"], "NOT_RESOLVED")
        self.assertIsNone(before["state"]["value"])
        self.assertIsNone(before["authority"])

        after = self.core.get_current_state(
            "project/DLC-01", "decision", "project:DLC-01",
            as_of="2026-10-25T00:00:00Z")
        self.assertStatus(after, "OK", "as_of with binding")
        self.assertEqual(after["authority"]["owner"], "user:owner")
        self.assertEqual(after["state"]["value"]["decision_id"], "D1")
        self.record(
            "T4R2-A1", "as_of 必须使用当时有效 Authority，不能套用今天 Owner",
            {"as_of": ["2026-10-15", "2026-10-25"]},
            {"before": "UNKNOWN/None", "after": "CURRENT/user:owner"},
            {"before": before["state"], "after": after["state"]},
        )


if __name__ == "__main__":
    unittest.main()
