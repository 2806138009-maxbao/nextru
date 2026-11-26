# Research Evidence read path (`get_research_evidence`)

A read-only operation for **non-Current research material** — the raw machine
transcripts and other DRAFT/HISTORICAL items that the CONCISE pack correctly
refuses to serve.

```
dispatch(core, "get_research_evidence", {
    "object_id": "video/AMI-001",
    "scope": "creator:ami.moment",
    "max_chars": 12000,          # optional
})
```

## Why it is a separate operation

`get_context_pack` CONCISE selects only **Current** aspects. Machine ASR is
stored as `DRAFT / UNVERIFIED / is_current=0`, so a CONCISE pack reports
`UNKNOWN` with 0 facts for it. That is the intended boundary and this task does
not change it: candidate research material must never be injected into the
formal context.

The research read path therefore exists **beside** the packs, not inside them.
It cannot promote anything: it has no write path, reads only
`DRAFT`/`HISTORICAL`/`SUPERSEDED` aspects, and labels every response
`is_fact=false / is_current=false / is_verified=false / promotion=FORBIDDEN`.

## Response

```jsonc
{
  "mode": "RESEARCH_EVIDENCE",
  "status": "OK",                       // execution status (worst of readings)
  "semantics": {
    "is_fact": false, "is_current": false, "is_verified": false,
    "promotion": "FORBIDDEN"
  },
  "object": {"id": "...", "type": "...", "name": "...", "scope": "..."},
  "readings": [{
    "aspect": {
      "stable_id": "aspect:video/AMI-001/raw_asr",
      "version_id": "video/AMI-001:raw_asr:r1",
      "revision": 1, "lifecycle": "DRAFT", "verification": "UNVERIFIED",
      "is_current": false, "promotion": "NONE",
      "authority": {"owner": "GOOGLE_DRIVE", "write_policy": "READ_ONLY", ...}
    },
    "identity": {
      "source_ref_id": "...", "source_system": "GOOGLE_DRIVE",
      "native_id": "...",
      "content": {
        "content_sha256": "...",            // bytes of the source document
        "sha256_scope": "SOURCE_DOCUMENT_BYTES",
        "reverified_from_section": "raw-json",
        "byte_length": 7996
      },
      "native_revision": {"value": null, "status": "NOT_CHECKED", ...},
      "anchor": "..."
    },
    "access": {"scope": "...", "access_scope": "...", "status": "OK"},
    "segments": [{
      "id": "obs/ami001/zh-0001", "index": 1,
      "anchor": "source_s:180.000-182.200",
      "text": "...", "stance": "TO_TEST",
      "source_start_s": 180.0, "source_end_s": 182.2,
      "clip_local_start_s": 0.0, "clip_local_end_s": 2.2,
      "text_review_status": "ASR_MACHINE_UNREVIEWED",
      "confidence_exp_logprob": 0.85, "text_matches_section": true
    }],
    "coverage": {
      "segment_count": 22, "segments_returned": 22,
      "payload_segment_count": 22, "payload_segment_count_match": true,
      "first_source_start_s": 180.0, "last_source_end_s": 234.96,
      "section_text_matched": 22, "section_text_mismatched": 0,
      "complete": true
    },
    "payload_meta": {"source_file_sha256": "...", "source_revision": "NOT_CHECKED", ...}
  }],
  "evidence_boundary": ["..."],
  "redaction": {"applied": false, "redactions": 0, "fields": []},
  "budget": {"max_chars": 12000, "chars": 10268, "exceeded": false,
             "truncated": false, "dropped_segments": []}
}
```

## Guarantees

- **Status truth.** Non-Current content is never reported as Current/VERIFIED.
  Segment stance stays whatever the store holds (`TO_TEST` for machine ASR).
  The read performs no write; tests assert the rows are byte-identical after it.
- **Stable identity.** `aspect:<object_id>/<aspect>` is the same id used by the
  context packs and now also resolves in `trace_evidence`.
- **Content hash ≠ native revision.** `content.content_sha256` hashes the source
  document bytes and is re-computed from the stored section when the full body
  is present (`reverified_from_section`). `native_revision.value` is the Drive
  revision and is `null` / `NOT_CHECKED` when the importer did not record one.
  A content hash is never reported as a revision.
- **Fidelity signals.** `coverage` proves the returned segment count against the
  payload's `segment_count`, and compares every segment text to the stored
  section body. A mismatch downgrades the reading to `PARTIAL` with
  `SECTION_TEXT_MISMATCH` / `SEGMENT_COUNT_MISMATCH`.
- **Scope is authorisation.** An omitted `scope` is `global`, never inferred
  from the object. A scoped object read as `global`, a wrong scope, or a
  SourceRef whose `access_scope` differs → `INVALID_SCOPE`, no segments.
  Blocked/missing sources → `BLOCKED`/`MISSING`, no cached content: `identity`
  is reduced to `source_ref_id` / `source_system` / `withheld` and the native id,
  anchor and content digest are not disclosed.
- **Default response is redacted.** It never includes the raw JSON document,
  internal filesystem paths, model/runtime/decode metadata, or other objects'
  content. Payload metadata is an allowlist; any residual path in served text
  is replaced with `[REDACTED_LOCAL]` and counted in `redaction`.
- **Budget is exact.** `budget.chars` is the canonical serialisation length.
  Dropping segments to fit sets `status=PARTIAL`, `truncated=true` and lists the
  dropped segment ids.

## Tests

`tests/test_task003_research_evidence.py` (27 tests): 22/22 segment read with
timecodes, coverage proof, DRAFT/UNVERIFIED/TO_TEST preservation, no mutation,
stable-id trace, content-hash vs native-revision separation, scope denial,
blocked source, unrelated-object exclusion, path/raw-JSON withholding,
redaction, budget truncation, section/count mismatch fidelity, and CONCISE
remaining Current-only.

`ami001-isolated-2026-10-31/run/research_acceptance.py` runs the same checks
against the private AMI-001 database read-through copy, with before/after
SHA-256 of the original database.
