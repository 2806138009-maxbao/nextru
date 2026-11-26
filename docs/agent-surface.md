# Agent read surface and its security boundary

This page describes exactly what a research Agent can reach through the local
transport (`nexus.service.dispatch` / loopback HTTP `/query`), and what it
cannot. It is the contract the Task 004 experiment relies on.

## 1. Operations exposed

`GET /health` returns:

```
resolve_object, list_objects, list_relation_types, get_current_state,
traverse_relations, trace_evidence, get_context, get_context_pack,
get_research_evidence
```

`POST /query` dispatches exactly these nine. `evaluate_event` and
`refresh_projection` are state-changing test operations; the read surface
answers `AUTHORITY_DENIED` and they are only available on `/test/query`, which a
server must enable explicitly (`--enable-test-api`, loopback only) and which
injects the caller principal itself.

`list_objects` and `list_relation_types` were added after Task 006: a cold-start
agent could not enumerate the corpus or guess the relation vocabulary, and it
concluded (wrongly) that the graph was empty. Both return **metadata only** —
object ids/types/names, relation types with a verification mix — and never
payloads, evidence or edge endpoints. They still obey scope: `global` sees only
global-scope rows.

## 2. What each operation may reveal

| Operation | Purpose | Leak surface |
|---|---|---|
| `resolve_object` | object id / candidates in scope | metadata only |
| `list_objects` | identity catalog (id/type/name) | metadata only; payloads withheld |
| `list_relation_types` | relation vocabulary + verification mix | no endpoints |
| `get_current_state` | Current value + resolution/verification/freshness | aspect payload (may embed operational metadata, **paths redacted**) |
| `traverse_relations` | verified 1–3 hop traversal (+ candidates) | edge metadata |
| `trace_evidence` | SourceRef + revision + anchor for an item | **no sections**: raw body expansion removed from the transport |
| `get_context` | intent-routed package | payloads, **paths redacted** |
| `get_context_pack` | CONCISE/DETAILED agent pack | payloads in DETAILED, **paths redacted**; section bodies never served |
| `get_research_evidence` | DRAFT/HISTORICAL segments + timecodes + identity | allowlisted fields only; no paths, no raw JSON |

## 3. What is not reachable

- **Raw source bodies.** `trace_evidence` no longer accepts `expand` at the
  transport (any value, including `false`, is rejected with `INVALID_SCOPE` and
  `allowed: ["item_id","scope"]`). Core keeps `expand` for the pack layer,
  which only uses it to compute section fingerprints.
- **Internal filesystem paths.** Every response passes through
  `nexus.redaction.scrub_local_paths`; a string that looks like a local path
  (`/home/…`, `/Users/…`, `/mnt/…`, `C:\…`, UNC shares, …) is replaced with
  `[REDACTED_LOCAL]` and the response gains
  `LOCAL_PATH_REDACTED: N internal path string(s) were replaced`. Research text,
  SHA-256 digests, anchors and URLs are not touched.
- **State-changing operations.** Not on the read surface at all.
- **Client-supplied principals.** `caller` / `authority_owner` are rejected;
  the test interface injects the principal server-side.

## 4. What is still readable (by design)

`get_current_state`, `get_context` and the DETAILED pack can return a stored
aspect payload. For the AMI-001 transcript that payload includes model and
decode metadata (model name, per-file hashes, decode parameters) but **no
machine paths**. This is research data the Agent is allowed to study; it is not
a path leak. `get_research_evidence` remains the safer default because it
returns an allowlisted projection of that payload.

## 5. Boundary of the claim

This is a **loopback pilot harness**, not production identity:

- `scope` is authorisation by convention, not authenticated identity;
- there is no per-user token, no audit trail beyond the local process log;
- an in-process caller can still use `NexusCore` directly, where `expand` and
  unredacted payloads are available;
- the HTTP surface is single-process, unauthenticated, and bound to 127.0.0.1.

Those limits are acceptable for an isolated experiment and are **not** a
production security claim.

## 6. Tests

`tests/test_task004_agent_surface.py` (13 tests) covers: `expand` rejected on
the transport (true/false/string), no sections in transport responses, Core
`expand` still available internally, payload paths redacted in
`get_current_state` and DETAILED, `LOCAL_PATH_REDACTED` reported, clean
responses unchanged, URLs/hashes/anchors untouched, and test operations still
refused.

`p1_task004_agent_read/audit_surface.py` is the empirical audit against the real
AMI-001 database: it probes every operation and fails the run if any response
contains a local path.
