"""Task 003 — Research Evidence read path (positive and negative tests).

Fixtures mirror the real AMI-001 shape: one DRAFT/UNVERIFIED machine-transcript
aspect, one SourceRef with a content SHA-256 and no native revision, 22
time-anchored observations, and 23 sections (full document + one per segment).

The tests are deliberately independent of the private AMI-001 database so the
suite stays reproducible; the real-data acceptance run lives in
`ami001-isolated-2026-10-31/run/research_acceptance.py`.
"""

from __future__ import annotations

import hashlib
import json

from helpers import NexusTest

from nexus.errors import Status
from nexus.service import dispatch

SCOPE = "creator:test.creator"
VIDEO = "video/T-001"
OTHER_VIDEO = "video/T-002"
SRC = "src/t-001/clip/asr-run"
SEGMENT_COUNT = 22
OFFSET = 180.0


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ResearchFixture(NexusTest):
    """Seeds an AMI-001-shaped research object inside an isolated scope."""

    leak_path = False

    def setUp(self) -> None:
        super().setUp()
        self._seed()

    # ------------------------------------------------------------------ seeding
    def _raw_document(self) -> dict:
        audio_path = ("/home/media/jobs/ami-win-001/audio.wav" if self.leak_path
                      else "audio.wav")
        segments = []
        for i in range(SEGMENT_COUNT):
            start = round(i * 1.8, 3)
            segments.append({
                "segment_id": f"zh-{i + 1:04d}",
                "clip_local_start_s": start,
                "clip_local_end_s": round(start + 1.8, 3),
                "text": f"片段文本 {i + 1}",
            })
        return {
            "schema_version": "ami-win-001.asr-run.v1",
            "source_file_id": "test-native-001",
            "source_file_sha256": None,  # filled after serialisation
            "source_revision": "NOT_CHECKED",
            "source_video_offset_s": OFFSET,
            "coverage": "ONE clip only",
            "transcript_status": "ASR_MACHINE_UNREVIEWED",
            "audio": {"path": audio_path,
                      "sha256": "a" * 64},
            "model": {"repo": "Systran/faster-whisper-small",
                      "path": "/home/media/models/faster-whisper-small",
                      "files_sha256": {"model.bin": "b" * 64}},
            "runtime": {"python": "3.12.14", "faster_whisper": "1.2.1"},
            "decode_params": {"beam_size": 5, "language": "zh"},
            "gpu": {"cuda_device_count": 1},
            "data": {"schema_version": "ami-win-001.asr-run.v1",
                     "segment_count": SEGMENT_COUNT,
                     "detected_language": "zh",
                     "language_probability": 1.0,
                     "segments": segments},
        }

    def _seed(self) -> None:
        store = self.store
        store.add_scope(SCOPE, "creator", "test scope")
        store.add_object(VIDEO, "Video", "T-001 research video", SCOPE)
        store.add_object(OTHER_VIDEO, "Video", "T-002 unrelated video", SCOPE)
        store.add_object("creator/test.creator", "Creator", "test.creator", SCOPE)

        doc = self._raw_document()
        body = json.dumps(doc, ensure_ascii=False, indent=2)
        content_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        doc["source_file_sha256"] = content_sha
        payload_body = json.dumps(doc, ensure_ascii=False)

        store.add_source_ref(SRC, "GOOGLE_DRIVE", "test-native-001", None,
                             "https://drive.google.com/file/d/test-native-001/view",
                             "clip_local=0..39.6; source≈180..219.6", SCOPE,
                             content=body)
        store.add_section(SRC, "raw-json", "Complete original ASR run JSON", body)

        for i in range(SEGMENT_COUNT):
            start = round(OFFSET + i * 1.8, 3)
            end = round(start + 1.8, 3)
            anchor = f"source_s:{start:.3f}-{end:.3f}"
            text = f"片段文本 {i + 1}"
            store.add_section(SRC, anchor, f"zh-{i + 1:04d}", text)
            store.add_observation(
                f"obs/t-001/zh-{i + 1:04d}", VIDEO, "asr_utterance", text,
                "TO_TEST",
                json.dumps({
                    "segment_id": f"zh-{i + 1:04d}",
                    "anchor": anchor,
                    "clip_local_start_s": round(i * 1.8, 3),
                    "clip_local_end_s": round(i * 1.8 + 1.8, 3),
                    "source_start_s": start,
                    "source_end_s": end,
                    "confidence_exp_logprob": 0.85,
                    "text_review_status": "ASR_MACHINE_UNREVIEWED",
                }, ensure_ascii=False),
                [SRC],
            )

        store.add_aspect(
            f"{VIDEO}:raw_asr:r1", VIDEO, "raw_asr", json.loads(payload_body),
            schema_version="1", lifecycle="DRAFT", verification="UNVERIFIED",
            revision=1, is_current=False, valid_from=None, valid_to=None,
            source_ref_id=SRC,
        )
        store.add_authority("auth/t-001/asr-source", "aspect", VIDEO, "raw_asr",
                            "GOOGLE_DRIVE", SCOPE, "test-native-001",
                            "2026-10-31T00:00:00Z", "READ_ONLY", "MIRRORED")

        # Unrelated content in the same scope: must never enter this response.
        store.add_observation("obs/t-002/unrelated", OTHER_VIDEO, "equipment",
                              "T-002 unrelated statement", "CASE", "{}", [SRC])
        # A Current aspect on the unrelated object must stay out of reach too.
        store.add_aspect(
            f"{OTHER_VIDEO}:equipment:r1", OTHER_VIDEO, "equipment",
            {"summary": "T-002 current equipment fact"}, schema_version="1",
            lifecycle="CURRENT", verification="VERIFIED", revision=1,
            is_current=True, valid_from="2026-01-01T00:00:00Z", valid_to=None,
            source_ref_id=None,
        )

    # ------------------------------------------------------------------ helpers
    def research(self, **params):
        base = {"object_id": VIDEO, "scope": SCOPE}
        base.update(params)
        return dispatch(self.core, "get_research_evidence", base)

    def reading(self, envelope, index: int = 0) -> dict:
        return (envelope.get("result") or {})["readings"][index]

    def assert_not_promoted(self) -> None:
        rows = self.store.conn.execute(
            "SELECT lifecycle, verification, is_current FROM aspects"
            " WHERE object_id = ?", (VIDEO,)).fetchall()
        self.assertTrue(rows, "research aspect must still exist")
        for row in rows:
            self.assertEqual(row["lifecycle"], "DRAFT")
            self.assertEqual(row["verification"], "UNVERIFIED")
            self.assertEqual(row["is_current"], 0)


class ResearchEvidenceTests(ResearchFixture):

    # ------------------------------------------------------- 1. complete read
    def test_returns_all_segments_with_timecodes(self) -> None:
        env = self.research()
        self.assertStatus(env, Status.OK)
        reading = self.reading(env)
        segments = reading["segments"]
        self.assertEqual(len(segments), SEGMENT_COUNT)
        self.assertEqual([s["index"] for s in segments], list(range(1, SEGMENT_COUNT + 1)))
        for i, segment in enumerate(segments):
            self.assertEqual(segment["id"], f"obs/t-001/zh-{i + 1:04d}")
            self.assertAlmostEqual(segment["source_start_s"], OFFSET + i * 1.8, places=3)
            self.assertAlmostEqual(segment["source_end_s"], OFFSET + (i + 1) * 1.8, places=3)
            self.assertAlmostEqual(segment["clip_local_start_s"], i * 1.8, places=3)
            self.assertTrue(segment["text_matches_section"])
            self.assertIn(f"片段文本 {i + 1}", segment["text"])

    def test_coverage_proves_22_of_22(self) -> None:
        reading = self.reading(self.research())
        coverage = reading["coverage"]
        self.assertEqual(coverage["segment_count"], SEGMENT_COUNT)
        self.assertEqual(coverage["segments_returned"], SEGMENT_COUNT)
        self.assertEqual(coverage["payload_segment_count"], SEGMENT_COUNT)
        self.assertTrue(coverage["payload_segment_count_match"])
        self.assertEqual(coverage["section_text_matched"], SEGMENT_COUNT)
        self.assertEqual(coverage["section_text_mismatched"], 0)
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["first_source_start_s"], OFFSET)
        self.assertAlmostEqual(coverage["last_source_end_s"],
                               OFFSET + SEGMENT_COUNT * 1.8, places=3)

    def test_dispatch_health_lists_the_read_operation(self) -> None:
        health = dispatch(self.core, "health")
        self.assertIn("get_research_evidence", health["operations"])

    # ------------------------------------------------------- 2. status truth
    def test_status_stays_draft_unverified_not_current(self) -> None:
        env = self.research()
        reading = self.reading(env)
        aspect = reading["aspect"]
        self.assertEqual(aspect["lifecycle"], "DRAFT")
        self.assertEqual(aspect["verification"], "UNVERIFIED")
        self.assertFalse(aspect["is_current"])
        self.assertEqual(aspect["promotion"], "NONE")
        self.assertEqual(aspect["stable_id"], f"aspect:{VIDEO}/raw_asr")
        for segment in reading["segments"]:
            self.assertEqual(segment["stance"], "TO_TEST")
            self.assertEqual(segment["text_review_status"], "ASR_MACHINE_UNREVIEWED")
        semantics = env["result"]["semantics"]
        self.assertFalse(semantics["is_fact"])
        self.assertFalse(semantics["is_current"])
        self.assertFalse(semantics["is_verified"])
        self.assertEqual(semantics["promotion"], "FORBIDDEN")

    def test_read_does_not_mutate_the_projection(self) -> None:
        before = {
            "aspects": self.store.conn.execute(
                "SELECT lifecycle, verification, is_current, revision FROM aspects"
                " ORDER BY version_id").fetchall(),
            "observations": self.store.conn.execute(
                "SELECT count(*) AS n FROM observations").fetchone()["n"],
            "relations": self.store.conn.execute(
                "SELECT count(*) AS n FROM relations").fetchone()["n"],
        }
        env = self.research()
        self.assertStatus(env, Status.OK)
        after = {
            "aspects": self.store.conn.execute(
                "SELECT lifecycle, verification, is_current, revision FROM aspects"
                " ORDER BY version_id").fetchall(),
            "observations": self.store.conn.execute(
                "SELECT count(*) AS n FROM observations").fetchone()["n"],
            "relations": self.store.conn.execute(
                "SELECT count(*) AS n FROM relations").fetchone()["n"],
        }
        self.assertEqual([tuple(r) for r in before["aspects"]],
                         [tuple(r) for r in after["aspects"]])
        self.assertEqual(before["observations"], after["observations"])
        self.assertEqual(before["relations"], after["relations"])
        self.assert_not_promoted()

    # ------------------------------------------------- 3. stable id + 回源
    def test_stable_aspect_id_resolves_in_trace_evidence(self) -> None:
        env = self.core.trace_evidence(f"aspect:{VIDEO}/raw_asr", SCOPE)
        self.assertStatus(env, Status.OK)
        item = env["result"]["item"]
        self.assertEqual(item["kind"], "aspect_version")
        self.assertEqual(item["aspect"]["name"], "raw_asr")
        self.assertEqual(item["aspect"]["version_id"], f"{VIDEO}:raw_asr:r1")
        self.assertEqual(item["aspect"]["lifecycle"], "DRAFT")
        self.assertEqual(item["aspect"]["verification"], "UNVERIFIED")
        self.assertFalse(item["aspect"]["is_current"])
        evidence = env["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["source_ref_id"], SRC)
        self.assertEqual(evidence[0]["revision"], None)

    def test_stable_aspect_id_respects_scope(self) -> None:
        denied = self.core.trace_evidence(f"aspect:{VIDEO}/raw_asr", "global")
        self.assertStatus(denied, Status.INVALID_SCOPE)
        self.assertIsNone(denied["result"])

    def test_unknown_stable_aspect_id_is_not_found(self) -> None:
        env = self.core.trace_evidence(f"aspect:{VIDEO}/not_a_real_aspect", SCOPE)
        self.assertStatus(env, Status.NOT_FOUND)
        self.assertIsNone(env["result"])

    def test_content_sha256_and_native_revision_are_distinct(self) -> None:
        reading = self.reading(self.research())
        content = reading["identity"]["content"]
        native = reading["identity"]["native_revision"]
        body = self.store.conn.execute(
            "SELECT body FROM source_sections WHERE source_ref_id = ? AND anchor = ?",
            (SRC, "raw-json")).fetchone()["body"]
        expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.assertEqual(content["content_sha256"], expected)
        self.assertEqual(content["sha256_scope"], "SOURCE_DOCUMENT_BYTES")
        self.assertEqual(content["reverified_from_section"], "raw-json")
        self.assertEqual(content["byte_length"], len(body.encode("utf-8")))
        self.assertIsNone(native["value"])
        self.assertEqual(native["status"], "NOT_CHECKED")
        # The content hash must never be reported as the native revision.
        self.assertNotEqual(native["value"], content["content_sha256"])
        self.assertIn("NATIVE_REVISION_NOT_RECORDED", " ".join(
            w for w in self.research()["warnings"]))

    def test_stored_content_hash_mismatch_is_not_claimed_as_verified(self) -> None:
        self.store.conn.execute(
            "UPDATE source_sections SET body = body || ' ' WHERE source_ref_id = ?"
            " AND anchor = 'raw-json'", (SRC,))
        reading = self.reading(self.research())
        content = reading["identity"]["content"]
        self.assertIsNone(content["reverified_from_section"])
        self.assertIsNotNone(content["content_sha256"])

    # ---------------------------------------------------------- 4. security
    def test_default_response_withholds_paths_and_raw_json(self) -> None:
        text = json.dumps(self.research(), ensure_ascii=False)
        for needle in ("/home/", "audio.wav", "faster-whisper", "model.bin",
                       "decode_params", "cuda_device_count", "beam_size",
                       "source_sections", '"audio"', '"model"', '"runtime"',
                       '"gpu"'):
            self.assertNotIn(needle, text)
        reading = self.reading(self.research())
        self.assertNotIn("audio", reading["payload_meta"])
        self.assertNotIn("model", reading["payload_meta"])
        self.assertNotIn("decode_params", reading["payload_meta"])
        self.assertIn("data", reading["payload_meta"])
        self.assertEqual(reading["payload_meta"]["data"]["segment_count"], SEGMENT_COUNT)

    def test_scope_is_required_for_a_scoped_object(self) -> None:
        env = dispatch(self.core, "get_research_evidence", {"object_id": VIDEO})
        self.assertStatus(env, Status.INVALID_SCOPE)
        self.assertIsNone(env["result"])
        self.assertNotIn("片段文本 1", json.dumps(env, ensure_ascii=False))

    def test_wrong_scope_is_rejected(self) -> None:
        self.store.add_scope("creator:someone.else", "creator")
        env = self.research(scope="creator:someone.else")
        self.assertStatus(env, Status.INVALID_SCOPE)
        self.assertIsNone(env["result"])
        self.assertNotIn("片段文本 1", json.dumps(env, ensure_ascii=False))

    def test_unknown_parameter_is_rejected(self) -> None:
        env = self.research(mode="CONCISE")
        self.assertStatus(env, Status.INVALID_SCOPE)
        self.assertIn("allowed", env["error"]["detail"])

    def test_missing_object_id_is_rejected(self) -> None:
        env = dispatch(self.core, "get_research_evidence",
                       {"scope": SCOPE})
        self.assertStatus(env, Status.INVALID_SCOPE)

    def test_blocked_source_serves_no_content(self) -> None:
        self.store.conn.execute(
            "UPDATE source_refs SET status = 'BLOCKED' WHERE source_ref_id = ?",
            (SRC,))
        env = self.research()
        self.assertStatus(env, Status.BLOCKED)
        reading = self.reading(env)
        self.assertEqual(reading["segments"], [])
        self.assertNotIn("payload_meta", reading)
        self.assertEqual(reading["coverage"]["segments_returned"], 0)
        self.assertNotIn("native_id", reading["identity"])
        self.assertNotIn("content", reading["identity"])
        self.assertIn("native_id", reading["identity"]["withheld"])
        self.assertNotIn("test-native-001", json.dumps(env, ensure_ascii=False))
        self.assertNotIn("片段文本 1", json.dumps(env, ensure_ascii=False))

    def test_source_ref_from_another_scope_serves_no_content(self) -> None:
        self.store.conn.execute(
            "UPDATE source_refs SET access_scope = 'creator:someone.else'"
            " WHERE source_ref_id = ?", (SRC,))
        env = self.research()
        self.assertStatus(env, Status.INVALID_SCOPE)
        reading = self.reading(env)
        self.assertEqual(reading["segments"], [])
        self.assertNotIn("native_id", reading["identity"])
        self.assertNotIn("片段文本 1", json.dumps(env, ensure_ascii=False))

    def test_unrelated_object_content_is_excluded(self) -> None:
        text = json.dumps(self.research(), ensure_ascii=False)
        self.assertNotIn("T-002 unrelated statement", text)
        self.assertNotIn("T-002 current equipment fact", text)
        ids = [s["id"] for s in self.reading(self.research())["segments"]]
        self.assertTrue(all(i.startswith("obs/t-001/") for i in ids))

    # ------------------------------------------------------- 5. semantics kept
    def test_only_non_current_aspects_are_read(self) -> None:
        reading = self.reading(self.research())
        self.assertEqual(reading["aspect"]["name"], "raw_asr")
        names = [r["aspect"]["name"] for r in self.research()["result"]["readings"]]
        self.assertEqual(names, ["raw_asr"])
        self.assertNotIn("equipment", names)

    def test_object_with_only_current_aspects_has_no_research_evidence(self) -> None:
        env = self.research(object_id=OTHER_VIDEO)
        self.assertStatus(env, Status.NOT_FOUND)
        self.assertEqual(env["error"]["detail"]["reason"], "NO_RESEARCH_EVIDENCE")
        self.assertIsNone(env["result"])

    def test_concise_stays_current_only_after_research_read(self) -> None:
        self.assertStatus(self.research(), Status.OK)
        concise = dispatch(self.core, "get_context_pack", {
            "question": "这个视频片段讲了哪些内容？",
            "object_id": VIDEO, "scope": SCOPE, "mode": "CONCISE",
            "max_chars": 6000,
        })
        # Research content never becomes a Current fact: CONCISE still answers
        # UNKNOWN with an empty result rather than injecting the 22 segments.
        self.assertStatus(concise, Status.UNKNOWN)
        self.assertIsNone(concise["result"])
        self.assertIn("object has no current aspects", concise["warnings"][0])

    # --------------------------------------------------------- 6. budget
    def test_budget_truncation_is_reported(self) -> None:
        full = self.research(max_chars=40000)["result"]["budget"]["chars"]
        budget = full - 1500
        env = self.research(max_chars=budget)
        self.assertStatus(env, Status.PARTIAL)
        result = env["result"]
        self.assertTrue(result["budget"]["truncated"])
        self.assertTrue(result["budget"]["dropped_segments"])
        self.assertLess(result["budget"]["chars"], full)
        self.assertFalse(result["budget"]["exceeded"])
        self.assertLessEqual(result["budget"]["chars"], budget)
        returned = sum(len(r["segments"]) for r in result["readings"])
        self.assertEqual(returned + len(result["budget"]["dropped_segments"]),
                         SEGMENT_COUNT)
        self.assertFalse(result["readings"][0]["coverage"]["complete"])
        self.assertEqual(result["readings"][0]["coverage"]["segments_returned"],
                         returned)

    # --------------------------------------------------------- 7. fidelity
    def test_segment_count_mismatch_is_partial(self) -> None:
        payload = self.store.conn.execute(
            "SELECT payload FROM aspects WHERE object_id = ? AND name = 'raw_asr'",
            (VIDEO,)).fetchone()["payload"]
        doc = json.loads(payload)
        doc["data"]["segment_count"] = SEGMENT_COUNT + 1
        self.store.conn.execute(
            "UPDATE aspects SET payload = ? WHERE object_id = ? AND name = 'raw_asr'",
            (_canon(doc), VIDEO))
        env = self.research()
        self.assertStatus(env, Status.PARTIAL)
        coverage = self.reading(env)["coverage"]
        self.assertFalse(coverage["payload_segment_count_match"])
        self.assertEqual(coverage["reason"], "SEGMENT_COUNT_MISMATCH")
        self.assertEqual(coverage["segments_returned"], SEGMENT_COUNT)

    def test_section_text_mismatch_is_partial(self) -> None:
        self.store.conn.execute(
            "UPDATE source_sections SET body = '被改动的文本' WHERE source_ref_id = ?"
            " AND anchor = 'source_s:180.000-181.800'", (SRC,))
        env = self.research()
        self.assertStatus(env, Status.PARTIAL)
        coverage = self.reading(env)["coverage"]
        self.assertEqual(coverage["section_text_mismatched"], 1)
        self.assertEqual(coverage["reason"], "SECTION_TEXT_MISMATCH")

    def test_missing_observations_reports_unknown(self) -> None:
        self.store.conn.execute("DELETE FROM observations WHERE object_id = ?", (VIDEO,))
        env = self.research()
        self.assertStatus(env, Status.UNKNOWN)
        reading = self.reading(env)
        self.assertEqual(reading["segments"], [])
        self.assertEqual(reading["coverage"]["reason"], "NO_SEGMENT_OBSERVATIONS")


class ResearchRedactionTests(ResearchFixture):
    """A leaked path anywhere in the projection must be scrubbed, not served."""

    leak_path = True

    def test_internal_path_in_statement_is_redacted(self) -> None:
        leaking = "音频来自 /home/media/jobs/ami-win-001/audio.wav"
        self.store.conn.execute(
            "UPDATE observations SET statement = ? WHERE observation_id = ?",
            (leaking, "obs/t-001/zh-0003"))
        self.store.conn.execute(
            "UPDATE source_sections SET body = ? WHERE source_ref_id = ?"
            " AND anchor = 'source_s:183.600-185.400'", (leaking, SRC))
        env = self.research()
        self.assertStatus(env, Status.OK)
        text = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("/home/media", text)
        segment = self.reading(env)["segments"][2]
        self.assertEqual(segment["text"], "[REDACTED_LOCAL]")
        self.assertTrue(segment["text_matches_section"])
        self.assertTrue(env["result"]["redaction"]["applied"])
        self.assertEqual(env["result"]["redaction"]["redactions"], 1)
        self.assertEqual(env["result"]["redaction"]["fields"][0],
                         "readings.segments.text")

    def test_payload_with_path_is_never_served_by_default(self) -> None:
        env = self.research()
        text = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("/home/media", text)
        self.assertNotIn("audio.wav", text)
        # The allowlist, not the scrubber, is what keeps payload paths out.
        self.assertFalse(env["result"]["redaction"]["applied"])
