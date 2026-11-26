# CONCISE / DETAILED context delivery

`get_context_pack` is a read-only operation with two return modes. The mode is
the **tool response format**, not chat wording.

## CONCISE (default)

```
dispatch(core, "get_context_pack", {
    "question": "...",              # natural-language question
    "object_id": "creator/...",     # optional; resolved from question if omitted
    "scope": "creator:...",
    "mode": "CONCISE",              # optional; this is the default
    "max_chars": 6000,
})
```

Returns one compact pack:

- `facts[]` — selected aspects with `id`, `statement`, `stance`, `authority`,
  `anchors`, and only the structured fields not already in the statement;
- `critical_exceptions[]` — decisive reversals from aspects outside the selected
  facets (e.g. Codex animation is slower but acceptable);
- `relations[]` — only when a relation is itself part of the answer (creator
  `AUTHORED` candidate);
- `authority[]`, `boundaries[]`, `unknowns[]`;
- `evidence.sources[]` — document revision + anchor count, not the full
  SourceRef metadata;
- `budget`.

It does **not** return the full object/aspect catalogue, repeated evidence
metadata, `request`/`execution` debugging fields, or `[REDACTED_LOCAL]` bodies.

## DETAILED (on demand)

```
dispatch(core, "get_context_pack", {
    "object_id": "creator/...",
    "scope": "creator:...",
    "mode": "DETAILED",
    "item_ids": ["aspect:creator/.../ai_workflow", "obs/CR-10"],
})
```

Expands only the requested stable ids to:

- full aspect payload + aspect revision + SourceRefs (native id, revision,
  location, anchor, status);
- observation statement/detail + section fingerprints (`content_sha256`,
  `content_available: false`);
- explicit `content_availability`: the projection does not contain paragraph
  text; it never passes a placeholder off as source text.

DETAILED reads the same projection as CONCISE, so a statement cannot change
between modes.

## Research material (separate operation)

DRAFT/HISTORICAL research material — e.g. a machine transcript stored as
`DRAFT / UNVERIFIED / is_current=0` — is **not** served by either mode. That is
deliberate: CONCISE is Current-only, so an object with no Current aspect answers
`UNKNOWN` with 0 facts.

Such content is readable through the separate read-only operation
`get_research_evidence(object_id, scope, max_chars)`, documented in
[`research-evidence.md`](research-evidence.md). It returns segments, timecodes
and evidence identity with `is_fact=false / is_current=false /
is_verified=false`, cannot promote anything, and never feeds a context pack.

## Guarantees

- **Scope is authorisation.** `scope` is never inferred from the target object.
  A scoped object requested with `scope="global"` (or with no scope) is rejected
  with `INVALID_SCOPE`; `global` never upgrades to a scoped read.
- **Status is propagated.** `execution.status` and `pack.status` preserve
  `BLOCKED`/`MISSING`/`PARTIAL`/`INVALID_SCOPE`; facts carry `resolution` only
  when it is not `CURRENT` (e.g. `HISTORICAL_ONLY`, `SNAPSHOT_ONLY`).
- **Authority comes from the resolved binding** (`entry["authority"]`), not from
  a payload convention; candidates are never promoted to effective facts.
- **Stable IDs are validated.** `aspect:<object_id>/<aspect>` must encode the
  requested object; stored observation IDs (`obs-...`) and projection IDs
  (`obs/...`) are both accepted. The same `aspect:` id also resolves in
  `trace_evidence`, so a pack fact, its evidence trace and the research read
  share one identity.
- **History is history.** `HISTORICAL_ONLY` values are returned from
  `historical_value` and marked; they are never presented as Current.
- **Budget is exact.** `budget.chars` equals the final canonical serialisation;
  `exceeded=false` means the serialized pack is within `max_chars`. If facts are
  dropped to fit, status is `PARTIAL`, `budget.truncated=true`, and
  `completeness.complete=false` with the dropped fact ids.
- **Observation ownership is checked.** DETAILED accepts an observation only if
  it belongs to the requested object or to an object reachable through an
  effective `AUTHORED` relation; same-scope but unrelated observations are
  rejected with `INVALID_SCOPE`.
- **Source identity includes revision.** Evidence pointers are grouped by
  `(native_id, revision)`, so different revisions are never merged.
- Read-only: no schema change, no new source access, `drive_api_calls=0`.
- `CONCISE` never truncates statements to hit `max_chars`; the same statement
  text is returned by `DETAILED` for the same fact id.

## Tests

`tests/test_context_pack.py` covers default mode, small output, detailed
expansion without contradiction, invalid mode, missing `item_ids`, and unknown
objects. `tests/test_context_pack_hardening.py` covers scope denial, status
propagation, stored type routing, Authority binding, stable IDs, historical
values, source revisions, observation ownership, budget truncation/completeness
and exact budget accounting.
`scripts/run_tests.py` runs the full suite.
