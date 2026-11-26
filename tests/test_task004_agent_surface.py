"""Task 004 §A — the Agent-facing read surface must not leak machine detail.

The transport (`nexus.service.dispatch`) is the Agent boundary.  Two rules are
tested here:

1. raw source bodies are not an Agent tool parameter (`expand` is gone);
2. any internal filesystem path inside a stored payload is replaced before the
   response leaves, and the replacement is reported as a warning.
"""

from __future__ import annotations

import json

from helpers import NexusTest

from nexus.core import NexusCore
from nexus.db import Store, connect
from nexus.errors import Status
from nexus.redaction import REDACTED, scrub_local_paths
from nexus.service import dispatch

DLC = "project/DLC-01"
DLC_SCOPE = "project:DLC-01"
LEAKY = "leak_probe"
LEAKY_PAYLOAD = {
    "note": "音频位于 /home/media/lifestack-media/jobs/ami-win-001/audio.wav",
    "model": {"path": "/home/media/lifestack-media/models/faster-whisper-small"},
    "url": "https://drive.google.com/file/d/example/view",
    "content_sha256": "169457cecf07e2ba84a1078d8a80e92534a878a830a401f9d269a4b7efd9581d",
    "timecode": "source_s:180.000-182.200",
}


class AgentSurfaceTests(NexusTest):

    def _seed_leaky_aspect(self) -> None:
        self.store.add_aspect(
            f"{DLC}:{LEAKY}:r1", DLC, LEAKY, json.loads(json.dumps(LEAKY_PAYLOAD)),
            schema_version="1", lifecycle="DRAFT", verification="UNVERIFIED",
            revision=1, is_current=False, valid_from=None, valid_to=None,
            source_ref_id=None,
        )

    # ------------------------------------------------- 1. raw body exposure
    def test_expand_is_not_a_read_operation_parameter(self) -> None:
        env = dispatch(self.core, "trace_evidence", {
            "item_id": "obs-sponsor-1", "scope": "creator:CREATOR-A", "expand": True})
        self.assertStatus(env, Status.INVALID_SCOPE)
        self.assertIn("expand", env["error"]["message"])
        self.assertEqual(env["error"]["detail"]["allowed"], ["item_id", "scope"])

    def test_expand_false_is_also_rejected(self) -> None:
        env = dispatch(self.core, "trace_evidence", {
            "item_id": "obs-sponsor-1", "scope": "creator:CREATOR-A", "expand": False})
        self.assertStatus(env, Status.INVALID_SCOPE)

    def test_transport_trace_returns_no_sections(self) -> None:
        env = dispatch(self.core, "trace_evidence", {
            "item_id": "obs-sponsor-1", "scope": "creator:CREATOR-A"})
        self.assertStatus(env, Status.OK)
        self.assertEqual(env["result"]["sections"], [])

    def test_core_expand_survives_for_internal_pack_use(self) -> None:
        env = self.core.trace_evidence(
            "obs-sponsor-1", scope="creator:CREATOR-A", expand=True)
        self.assertStatus(env, Status.OK)
        self.assertTrue(env["request"]["expand"])
        self.assertIn("sections", env["result"])
        # DETAILED pack expansion goes through this Core call path.
        pack = dispatch(self.core, "get_context_pack", {
            "object_id": "video/CREATOR-A-V001", "scope": "creator:CREATOR-A",
            "mode": "DETAILED", "item_ids": ["obs-sponsor-1"]})
        self.assertStatus(pack, Status.OK)

    def test_read_surface_does_not_list_expand(self) -> None:
        health = dispatch(self.core, "health")
        self.assertIn("trace_evidence", health["operations"])
        env = dispatch(self.core, "trace_evidence", {
            "item_id": "x", "scope": "global", "expand": "yes"})
        self.assertStatus(env, Status.INVALID_SCOPE)

    # ------------------------------------------------------- 2. path scrub
    def test_payload_path_is_redacted_on_the_way_out(self) -> None:
        self._seed_leaky_aspect()
        env = dispatch(self.core, "get_current_state", {
            "object_id": DLC, "aspect": LEAKY, "scope": DLC_SCOPE})
        text = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("/home/", text)
        self.assertNotIn("lifestack-media", text)
        self.assertIn(REDACTED, text)
        self.assertTrue(any(w.startswith("LOCAL_PATH_REDACTED")
                            for w in env["warnings"]))
        entry = env["result"]["aspects"][0]
        payload = entry["historical_value"]
        self.assertEqual(payload["note"], REDACTED)
        self.assertEqual(payload["model"]["path"], REDACTED)
        # Non-path content survives untouched.
        self.assertEqual(payload["url"], LEAKY_PAYLOAD["url"])
        self.assertEqual(payload["content_sha256"], LEAKY_PAYLOAD["content_sha256"])
        self.assertEqual(payload["timecode"], LEAKY_PAYLOAD["timecode"])

    def test_detailed_pack_payload_is_redacted_too(self) -> None:
        self._seed_leaky_aspect()
        env = dispatch(self.core, "get_context_pack", {
            "object_id": DLC, "scope": DLC_SCOPE, "mode": "DETAILED",
            "item_ids": [f"aspect:{DLC}/{LEAKY}"]})
        text = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("/home/", text)
        self.assertIn(REDACTED, text)
        item = env["result"]["items"][0]
        self.assertEqual(item["payload"]["model"]["path"], REDACTED)

    def test_clean_response_has_no_redaction_warning(self) -> None:
        env = dispatch(self.core, "get_current_state", {
            "object_id": DLC, "aspect": "decision", "scope": DLC_SCOPE})
        self.assertStatus(env, Status.OK)
        self.assertFalse(any(w.startswith("LOCAL_PATH_REDACTED")
                             for w in env["warnings"]))

    def test_research_evidence_response_is_not_double_scrubbed(self) -> None:
        # No research aspect on the fixture DB: still a clean error envelope.
        env = dispatch(self.core, "get_research_evidence", {
            "object_id": DLC, "scope": DLC_SCOPE})
        self.assertStatus(env, Status.NOT_FOUND)
        self.assertFalse(any(w.startswith("LOCAL_PATH_REDACTED")
                             for w in env["warnings"]))

    def test_write_surface_still_refused_on_read_dispatch(self) -> None:
        for op in ("evaluate_event", "refresh_projection"):
            env = dispatch(self.core, op, {"event_id": "x", "scope": DLC_SCOPE})
            self.assertStatus(env, Status.AUTHORITY_DENIED)


class RedactionUnitTests(NexusTest):

    def test_detects_common_internal_locations(self) -> None:
        for value in ("/home/media/x.wav", "/Users/alice/x", "C:\\Users\\alice\\x",
                      "\\\\server\\share\\x", "/tmp/x", "/opt/models/x"):
            cleaned, count, _ = scrub_local_paths({"v": value})
            self.assertEqual(cleaned["v"], REDACTED, value)
            self.assertEqual(count, 1, value)

    def test_leaves_research_text_hashes_and_urls_alone(self) -> None:
        value = {
            "text": "窗边放了一盆绿萝，光线很好",
            "sha256": "169457cecf07e2ba84a1078d8a80e92534a878a830a401f9d269a4b7efd9581d",
            "url": "https://drive.google.com/file/d/1EXAMPLEdriveFILEid000000000000000/view",
            "anchor": "source_s:180.000-182.200",
            "ratio": 0.8549,
        }
        cleaned, count, fields = scrub_local_paths(value)
        self.assertEqual(cleaned, value)
        self.assertEqual(count, 0)
        self.assertEqual(fields, [])

    def test_counts_nested_occurrences(self) -> None:
        value = {"a": ["/home/a", "ok", "/home/b"], "b": {"c": "/tmp/c"}}
        cleaned, count, fields = scrub_local_paths(value)
        self.assertEqual(count, 3)
        self.assertEqual(cleaned["a"], [REDACTED, "ok", REDACTED])
        self.assertEqual(cleaned["b"]["c"], REDACTED)
        self.assertEqual(fields, ["a", "b.c"])
